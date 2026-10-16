"""Policy guardrails: autonomy tier, token budget, reasoning budget, human-decision policy."""
from __future__ import annotations

from dataclasses import dataclass

from blackboard.control_plane import ControlPlaneViolation, validate_human_decision, validate_llm_purpose
from guardrails.pipeline import GuardrailContext, Stage, block, ok, warn
from schemas.differential import HumanDecision
from schemas.execution import GuardrailResult


@dataclass
class LLMIntent:
    purpose: str
    prompt_chars: int
    projected_tokens: int
    max_input_tokens: int


class AutonomyTierPolicy:
    name = "autonomy_tier"
    stages = frozenset({Stage.INPUT})

    def check(self, subject: LLMIntent, ctx: GuardrailContext, stage: Stage) -> GuardrailResult:
        try:
            validate_llm_purpose(ctx.agent, subject.purpose)
        except ControlPlaneViolation as exc:
            return block(stage, self.name, str(exc))
        return ok(stage, self.name, f"{ctx.agent} may use an LLM for {subject.purpose}")


class TokenBudgetPolicy:
    name = "token_budget"
    stages = frozenset({Stage.TOKEN_BUDGET})

    def __init__(self, max_prompt_chars: int) -> None:
        self.max_prompt_chars = max_prompt_chars

    def check(self, subject: LLMIntent, ctx: GuardrailContext, stage: Stage) -> GuardrailResult:
        if subject.prompt_chars > self.max_prompt_chars:
            return block(stage, self.name, f"prompt {subject.prompt_chars} chars > limit {self.max_prompt_chars}")
        if subject.projected_tokens > subject.max_input_tokens:
            return block(stage, self.name, f"prompt {subject.projected_tokens} tokens > model limit {subject.max_input_tokens}")
        if subject.projected_tokens > 0.8 * subject.max_input_tokens:
            return warn(stage, self.name, "prompt above 80% of the model's input limit")
        return ok(stage, self.name)


@dataclass
class ReasoningBudget:
    depth: int
    branches: int
    max_depth: int
    max_branches: int
    elapsed_s: float
    time_budget_s: float


class ReasoningBudgetPolicy:
    """No infinite reasoning: every ATS expansion is checked against its bounds."""

    name = "reasoning_budget"
    stages = frozenset({Stage.REASONING_BUDGET})

    def check(self, subject: ReasoningBudget, ctx: GuardrailContext, stage: Stage) -> GuardrailResult:
        if subject.depth > subject.max_depth:
            return block(stage, self.name, f"depth {subject.depth} > {subject.max_depth}")
        if subject.branches > subject.max_branches:
            return block(stage, self.name, f"branches {subject.branches} > {subject.max_branches}")
        if subject.elapsed_s > subject.time_budget_s:
            return block(stage, self.name, f"time {subject.elapsed_s:.1f}s > {subject.time_budget_s}s")
        return ok(stage, self.name)


@dataclass
class DecisionSubject:
    item_type: str
    decision: HumanDecision


class HumanDecisionPolicy:
    name = "human_decision"
    stages = frozenset({Stage.HUMAN_POLICY})

    def check(self, subject: DecisionSubject, ctx: GuardrailContext, stage: Stage) -> GuardrailResult:
        try:
            validate_human_decision(subject.item_type, subject.decision.role, subject.decision.decided_by)
        except ControlPlaneViolation as exc:
            return block(stage, self.name, str(exc))
        if ctx.extra.get("environment") == "production" and subject.decision.decided_by.startswith("golden-dataset:"):
            return block(stage, self.name, "evaluation fixtures may never decide in production")
        return ok(stage, self.name, f"decision by {subject.decision.role.value}")
