"""Public-API LLM providers: OpenAI, Anthropic (Claude) and Google Gemini.

All three implement the same ``LLMProvider`` contract as Azure AI Foundry, so agent code never changes - only
``llm.provider`` in configuration does. Keys come from the environment only (``OPENAI_API_KEY``,
``ANTHROPIC_API_KEY``, ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``, or ``AIWB_LLM_API_KEY``); they are never
read from config files or logged.

These endpoints are outside the approved Azure environment (§34). ``build_provider`` refuses them when
``environment=production`` unless ``llm.allow_external_providers`` is explicitly true. Whatever the provider,
every call still passes the gateway: PII redaction, injection guardrails, Headroom, budgets, cost accounting,
structured-output validation - and the output is still untrusted until the deterministic guard accepts it.
"""
from __future__ import annotations

import time

import httpx

from config.settings import secret
from reasoning.llm.provider import (
    LLMError, LLMModel, LLMProvider, LLMRequest, LLMResponse, LLMTransientError, LLMUnavailable, LLMUsage,
)

TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def _key(*names: str) -> str:
    for n in (*names, "AIWB_LLM_API_KEY"):
        if v := secret(n):
            return v
    raise LLMUnavailable(f"no API key: set one of {', '.join((*names, 'AIWB_LLM_API_KEY'))}")


def _split(messages: list[dict]) -> tuple[str, str]:
    """Gateway messages -> (system text, one user turn). Evidence blocks stay in order inside the user turn."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    user = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
    return system, user


class _HttpProvider(LLMProvider):
    def __init__(self, timeout: float = 60, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def _post(self, url: str, headers: dict, body: dict, model: LLMModel) -> tuple[dict, float]:
        started = time.perf_counter()
        try:
            resp = self._client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise LLMTransientError(f"timeout calling {self.name}/{model.deployment}") from exc
        except httpx.HTTPError as exc:
            raise LLMTransientError(f"{self.name}: {exc}") from exc
        latency = (time.perf_counter() - started) * 1000
        if resp.status_code in TRANSIENT_STATUS:
            raise LLMTransientError(f"{resp.status_code} from {self.name}")
        if resp.status_code in (401, 403):
            raise LLMUnavailable(f"{self.name} rejected the API key ({resp.status_code})")
        if resp.status_code >= 400:
            raise LLMError(f"{resp.status_code} from {self.name}: {resp.text[:300]}")
        return resp.json(), latency

    @staticmethod
    def _model_id(model: LLMModel) -> str:
        if not model.deployment:
            raise LLMUnavailable(f"model profile {model.name} has no model id (deployment) configured")
        return model.deployment


class OpenAIProvider(_HttpProvider):
    name = "openai"

    def complete(self, request: LLMRequest, model: LLMModel) -> LLMResponse:
        body: dict = {"model": self._model_id(model), "messages": request.messages,
                      "max_completion_tokens": request.max_output_tokens or model.max_output_tokens}
        if model.temperature is not None:
            body["temperature"] = model.temperature
        if request.response_format == "json":
            body["response_format"] = {"type": "json_object"}
        base = (model.endpoint or "https://api.openai.com/v1").rstrip("/")
        data, latency = self._post(f"{base}/chat/completions", {"Authorization": f"Bearer {_key('OPENAI_API_KEY')}"},
                                   body, model)
        usage = data.get("usage", {})
        return LLMResponse(
            content=data["choices"][0]["message"].get("content") or "",
            usage=LLMUsage(input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
                           cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)),
            model=model.name, provider=self.name, latency_ms=latency,
            finish_reason=data["choices"][0].get("finish_reason", "stop"))


class AnthropicProvider(_HttpProvider):
    name = "anthropic"

    def complete(self, request: LLMRequest, model: LLMModel) -> LLMResponse:
        system, user = _split(request.messages)
        if request.response_format == "json":
            system += "\n\nRespond with a single JSON object only - no prose before or after it."
        body: dict = {"model": self._model_id(model), "max_tokens": request.max_output_tokens or model.max_output_tokens,
                      "system": system, "messages": [{"role": "user", "content": user}]}
        if model.temperature is not None:
            body["temperature"] = model.temperature
        base = (model.endpoint or "https://api.anthropic.com").rstrip("/")
        data, latency = self._post(f"{base}/v1/messages", {"x-api-key": _key("ANTHROPIC_API_KEY"),
                                                            "anthropic-version": "2023-06-01"}, body, model)
        usage = data.get("usage", {})
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        cached = usage.get("cache_read_input_tokens", 0) or 0
        return LLMResponse(
            content=text,
            usage=LLMUsage(input_tokens=usage.get("input_tokens", 0) + cached, output_tokens=usage.get("output_tokens", 0),
                           cached_tokens=cached),
            model=model.name, provider=self.name, latency_ms=latency, finish_reason=data.get("stop_reason") or "stop")


class GeminiProvider(_HttpProvider):
    name = "gemini"

    def complete(self, request: LLMRequest, model: LLMModel) -> LLMResponse:
        system, user = _split(request.messages)
        config: dict = {"maxOutputTokens": request.max_output_tokens or model.max_output_tokens}
        if model.temperature is not None:
            config["temperature"] = model.temperature
        if request.response_format == "json":
            config["responseMimeType"] = "application/json"
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": config}
        base = (model.endpoint or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        data, latency = self._post(f"{base}/models/{self._model_id(model)}:generateContent",
                                   {"x-goog-api-key": _key("GEMINI_API_KEY", "GOOGLE_API_KEY")}, body, model)
        cands = data.get("candidates") or []
        if not cands:
            raise LLMError(f"gemini returned no candidates: {data.get('promptFeedback', {})}")
        text = "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))
        usage = data.get("usageMetadata", {})
        return LLMResponse(
            content=text,
            usage=LLMUsage(input_tokens=usage.get("promptTokenCount", 0), output_tokens=usage.get("candidatesTokenCount", 0),
                           cached_tokens=usage.get("cachedContentTokenCount", 0)),
            model=model.name, provider=self.name, latency_ms=latency,
            finish_reason=str(cands[0].get("finishReason", "STOP")).lower())


EXTERNAL = {"openai": OpenAIProvider, "anthropic": AnthropicProvider, "gemini": GeminiProvider}
