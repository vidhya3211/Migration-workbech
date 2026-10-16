"""Provider/model selection from configuration only (§30).

``llm.provider`` picks the provider; ``llm.default_model`` picks the model profile. With
``default_model: auto`` the first profile whose ``provider`` matches ``llm.provider`` is used, so switching
provider is a single setting.
"""
from __future__ import annotations

from config.settings import Settings
from reasoning.llm.provider import LLMModel, LLMProvider
from reasoning.llm.providers import AzureFoundryProvider, OfflineProvider, ScriptedProvider
from reasoning.llm.public_providers import EXTERNAL


class ProviderNotPermitted(ValueError):
    pass


def build_provider(settings: Settings, scripted: ScriptedProvider | None = None) -> LLMProvider:
    name = settings.llm.provider
    if name == "offline":
        return OfflineProvider()
    if name == "scripted":
        return scripted or ScriptedProvider()
    if name == "azure_foundry":
        return AzureFoundryProvider(timeout=settings.llm.timeout_seconds)
    if name in EXTERNAL:
        if settings.environment == "production" and not settings.llm.allow_external_providers:
            raise ProviderNotPermitted(
                f"'{name}' is a public endpoint outside the approved Azure environment; set "
                "llm.allow_external_providers only with a documented data-protection approval")
        return EXTERNAL[name](timeout=settings.llm.timeout_seconds)
    raise ValueError(f"unknown LLM provider {name!r}")


def resolve_model(settings: Settings, name: str | None = None) -> LLMModel:
    key = name or settings.llm.default_model
    if key == "auto":
        key = next((k for k, m in settings.llm.models.items() if m.provider == settings.llm.provider), None)
        if key is None and settings.llm.provider in ("openai", "anthropic", "gemini", "azure_foundry"):
            raise ValueError(f"no model profile configured for provider {settings.llm.provider!r}")
        key = key or next(iter(settings.llm.models))   # offline / scripted: profile only used for accounting
    cfg = settings.llm.models.get(key)
    if cfg is None:
        raise ValueError(f"model {key!r} is not configured")
    return LLMModel(name=key, **cfg.model_dump())
