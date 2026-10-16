"""OpenAI / Anthropic / Gemini adapters through the real gateway, against mocked HTTP (no keys, no network)."""
import json

import httpx
import pytest
from pydantic import BaseModel

from config.settings import load_settings
from context.headroom.policy import ContentClass
from reasoning.llm.factory import ProviderNotPermitted, build_provider
from reasoning.llm.gateway import Evidence, LLMGateway
from reasoning.llm.public_providers import AnthropicProvider, GeminiProvider, OpenAIProvider

ANSWER = '{"answer": "The certified fee is 25.00.", "citations": ["rule:R-1"], "refused": false}'


class Answer(BaseModel):
    answer: str
    citations: list[str] = []
    refused: bool = False


def openai_reply(req: httpx.Request) -> httpx.Response:
    assert req.url.path == "/v1/chat/completions" and req.headers["authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["model"] == "gpt-4.1-mini" and body["response_format"] == {"type": "json_object"}
    return httpx.Response(200, json={"choices": [{"message": {"content": ANSWER}, "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 120, "completion_tokens": 20,
                                               "prompt_tokens_details": {"cached_tokens": 100}}})


def anthropic_reply(req: httpx.Request) -> httpx.Response:
    assert req.url.path == "/v1/messages" and req.headers["x-api-key"] == "sk-ant-test"
    assert req.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(req.content)
    assert body["model"] == "claude-sonnet-5" and "temperature" not in body and body["system"]
    assert [m["role"] for m in body["messages"]] == ["user"]
    return httpx.Response(200, json={"content": [{"type": "text", "text": ANSWER}], "stop_reason": "end_turn",
                                     "usage": {"input_tokens": 110, "output_tokens": 18, "cache_read_input_tokens": 0}})


def gemini_reply(req: httpx.Request) -> httpx.Response:
    assert req.url.path.endswith("/models/gemini-2.5-flash:generateContent") and req.headers["x-goog-api-key"] == "g-test"
    body = json.loads(req.content)
    assert body["generationConfig"]["responseMimeType"] == "application/json" and body["systemInstruction"]
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": ANSWER}]}, "finishReason": "STOP"}],
                                     "usageMetadata": {"promptTokenCount": 130, "candidatesTokenCount": 22}})


CASES = [("openai", OpenAIProvider, openai_reply, "OPENAI_API_KEY", "sk-test"),
         ("anthropic", AnthropicProvider, anthropic_reply, "ANTHROPIC_API_KEY", "sk-ant-test"),
         ("gemini", GeminiProvider, gemini_reply, "GEMINI_API_KEY", "g-test")]


@pytest.mark.parametrize("name,cls,handler,env,key", CASES)
def test_public_provider_through_the_governed_gateway(ws, monkeypatch, name, cls, handler, env, key):
    monkeypatch.setenv(env, key)
    settings = load_settings(overrides={"llm.provider": name})
    sent: list[str] = []

    def spy(req: httpx.Request) -> httpx.Response:
        sent.append(req.content.decode())
        return handler(req)
    gw = LLMGateway(settings, cls(transport=httpx.MockTransport(spy)), ws.services.headroom, ws.services.guardrails)
    res = gw.call(agent="assistant_agent", run_id="R", purpose="compose_cited_answer",
                  variables={"question": "What is the fee for jane.doe@example.com?"},
                  evidence=[Evidence("rule:R-1", "[rule:R-1] ADMIN FEE 25.00", ContentClass.SEARCH_RESULT)],
                  output_model=Answer)
    assert res.record.status == "ok" and res.parsed.answer.startswith("The certified fee")
    assert res.record.provider == name and res.record.output_tokens > 0 and float(res.record.estimated_cost) > 0
    assert "jane.doe@example.com" not in sent[0]           # PII redacted before it leaves the platform


def test_missing_key_is_unavailable_not_a_crash(ws, monkeypatch):
    for v in ("OPENAI_API_KEY", "AIWB_LLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    settings = load_settings(overrides={"llm.provider": "openai"})
    gw = LLMGateway(settings, OpenAIProvider(transport=httpx.MockTransport(openai_reply)), ws.services.headroom,
                    ws.services.guardrails)
    res = gw.call(agent="assistant_agent", run_id="R", purpose="compose_cited_answer", variables={"question": "q"},
                  evidence=[], output_model=Answer)
    assert res.parsed is None and res.record.status == "unavailable"


def test_public_providers_refused_in_production_without_approval():
    with pytest.raises(ProviderNotPermitted):
        build_provider(load_settings(overrides={"llm.provider": "anthropic", "environment": "production"}))
    ok = build_provider(load_settings(overrides={"llm.provider": "anthropic", "environment": "production",
                                                 "llm.allow_external_providers": True}))
    assert isinstance(ok, AnthropicProvider)
