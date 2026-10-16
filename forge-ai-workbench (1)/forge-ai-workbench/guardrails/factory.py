"""Builds the platform guardrail pipeline from configuration."""
from __future__ import annotations

from config.settings import Settings
from guardrails.injection.detector import InjectionPolicy
from guardrails.output.policies import (
    CitationPolicy, GroundedNarrativePolicy, OutputSchemaPolicy, UnsupportedClaimPolicy,
)
from guardrails.pii.detector import PiiPolicy
from guardrails.pipeline import GuardrailPipeline, Recorder
from guardrails.policy.policies import (
    AutonomyTierPolicy, HumanDecisionPolicy, ReasoningBudgetPolicy, TokenBudgetPolicy,
)
from guardrails.provenance.policies import AuditCompletenessPolicy
from guardrails.security.code_policy import UnsafeCodePolicy
from guardrails.tool.policies import ToolArgumentPolicy, ToolAuthorizationPolicy


def build_pipeline(settings: Settings, recorder: Recorder | None = None) -> GuardrailPipeline:
    g = settings.guardrails
    return GuardrailPipeline([
        InjectionPolicy(g.injection_block_threshold),
        PiiPolicy(),
        AutonomyTierPolicy(),
        TokenBudgetPolicy(g.max_prompt_chars),
        ReasoningBudgetPolicy(),
        ToolAuthorizationPolicy(),
        ToolArgumentPolicy(),
        OutputSchemaPolicy(),
        CitationPolicy(),
        UnsupportedClaimPolicy(),
        GroundedNarrativePolicy(),
        UnsafeCodePolicy(),
        AuditCompletenessPolicy(),
        HumanDecisionPolicy(),
    ], recorder=recorder)
