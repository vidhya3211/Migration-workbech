"""LLM provider implementations.

* ``OfflineProvider``      - no model available (default locally): raises ``LLMUnavailable`` so agents
                             take their deterministic paths. Honest: it never fabricates output.
* ``ScriptedProvider``     - deterministic canned responses keyed by prompt id (tests and evals).
* ``AzureFoundryProvider`` - Azure AI Foundry / Azure OpenAI chat completions over the private endpoint,
                             authenticated with Managed Identity (Entra ID) or a Key Vault-sourced key.
"""
from __future__ import annotations

import json
import time
from typing import Callable

import httpx

from config.settings import secret
from reasoning.llm.provider import (
    LLMError, LLMModel, LLMProvider, LLMRequest, LLMResponse, LLMTransientError, LLMUnavailable, LLMUsage,
)


class OfflineProvider(LLMProvider):
    name = "offline"

    def complete(self, request: LLMRequest, model: LLMModel) -> LLMResponse:
        raise LLMUnavailable("no LLM provider is configured for this environment")


class ScriptedProvider(LLMProvider):
    name = "scripted"

    def __init__(self, responses: dict[str, str | Callable[[LLMRequest], str]] | None = None) -> None:
        self.responses = responses or {}
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest, model: LLMModel) -> LLMResponse:
        self.requests.append(request)
        handler = self.responses.get(request.prompt_id)
        if handler is None:
            raise LLMUnavailable(f"no scripted response for {request.prompt_id}")
        content = handler(request) if callable(handler) else handler
        prompt = " ".join(str(m.get("content", "")) for m in request.messages)
        return LLMResponse(content=content, usage=LLMUsage(input_tokens=max(1, len(prompt) // 4),
                                                           output_tokens=max(1, len(content) // 4)),
                           model=model.name, provider=self.name, latency_ms=1.0)


class AzureFoundryProvider(LLMProvider):  # pragma: no cover - requires an Azure endpoint
    name = "azure_foundry"

    def __init__(self, timeout: float = 60) -> None:
        self.timeout = timeout
        self._credential = None

    def _auth_headers(self) -> dict[str, str]:
        key = secret("AIWB_LLM_API_KEY")
        if key:
            return {"api-key": key}
        if self._credential is None:
            from azure.identity import DefaultAzureCredential   # Managed Identity in AKS
            self._credential = DefaultAzureCredential()
        token = self._credential.get_token("https://cognitiveservices.azure.com/.default").token
        return {"Authorization": f"Bearer {token}"}

    def complete(self, request: LLMRequest, model: LLMModel) -> LLMResponse:
        if not model.endpoint or not model.deployment:
            raise LLMUnavailable(f"model {model.name} has no endpoint/deployment configured")
        url = f"{model.endpoint.rstrip('/')}/openai/deployments/{model.deployment}/chat/completions"
        body = {"messages": request.messages, "temperature": model.temperature,
                "max_tokens": request.max_output_tokens or model.max_output_tokens}
        if request.response_format == "json":
            body["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        try:
            resp = httpx.post(url, params={"api-version": model.api_version}, json=body,
                              headers=self._auth_headers(), timeout=self.timeout)
        except httpx.TimeoutException as exc:
            raise LLMTransientError(f"timeout calling {model.name}") from exc
        except httpx.HTTPError as exc:
            raise LLMTransientError(str(exc)) from exc
        if resp.status_code in (429, 500, 502, 503, 504):
            raise LLMTransientError(f"{resp.status_code} from {model.name}")
        if resp.status_code >= 400:
            raise LLMError(f"{resp.status_code} from {model.name}: {resp.text[:300]}")
        data = resp.json()
        usage = data.get("usage", {})
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        return LLMResponse(
            content=data["choices"][0]["message"]["content"] or "",
            usage=LLMUsage(input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
                           cached_tokens=cached),
            model=model.name, provider=self.name, latency_ms=(time.perf_counter() - started) * 1000,
            finish_reason=data["choices"][0].get("finish_reason", "stop"),
        )


def dumps(obj: object) -> str:
    return json.dumps(obj, default=str)
