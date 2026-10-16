"""Provider-independent LLM contracts (§30). Agent code never names a provider, model, key or endpoint."""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from pydantic import BaseModel, Field


class LLMModel(BaseModel):
    name: str                       # logical model name from configuration (e.g. "workbench-reasoning")
    provider: str
    deployment: str = ""
    endpoint: str = ""
    api_version: str = ""
    temperature: float | None = 0.0
    max_input_tokens: int = 32000
    max_output_tokens: int = 2048
    input_cost_per_1k: Decimal = Decimal("0")
    output_cost_per_1k: Decimal = Decimal("0")
    cached_cost_per_1k: Decimal = Decimal("0")


class LLMRequest(BaseModel):
    purpose: str
    prompt_id: str
    prompt_version: str
    messages: list[dict]
    response_format: str = "json"   # json | text
    max_output_tokens: int | None = None
    metadata: dict = Field(default_factory=dict)


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


class LLMResponse(BaseModel):
    content: str
    usage: LLMUsage
    model: str
    provider: str
    latency_ms: float
    finish_reason: str = "stop"


class LLMError(RuntimeError):
    retryable = False


class LLMUnavailable(LLMError):
    """No model is configured/reachable. Callers fall back to deterministic paths - never to approval."""


class LLMTransientError(LLMError):
    retryable = True


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def complete(self, request: LLMRequest, model: LLMModel) -> LLMResponse: ...
