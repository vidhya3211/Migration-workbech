"""Centralised guardrail pipeline (§14-15).

    Input -> [Input] -> Agent -> [Tool | MCP | Token budget | Reasoning budget | Output schema]
          -> [Deterministic guard] -> [Provenance] -> [Audit] -> [Human policy] -> Knowledge core

A failure is explicit: ``GuardrailViolation`` is raised and the event is recorded. Nothing silently
continues. ``warn`` results are recorded but do not stop the flow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Protocol

from observability.logging.logger import Event, log_event
from observability.tracing.tracer import SpanKind, get_tracer
from schemas.execution import GuardrailResult


class Stage(StrEnum):
    INPUT = "input"
    TOOL = "tool"
    MCP = "mcp"
    TOKEN_BUDGET = "token_budget"
    REASONING_BUDGET = "reasoning_budget"
    OUTPUT_SCHEMA = "output_schema"
    DETERMINISTIC = "deterministic"
    PROVENANCE = "provenance"
    AUDIT = "audit"
    HUMAN_POLICY = "human_policy"


class GuardrailViolation(Exception):
    def __init__(self, results: list[GuardrailResult]) -> None:
        blocked = [r for r in results if not r.passed and r.severity == "block"]
        super().__init__("; ".join(f"[{r.stage}/{r.policy}] {r.reason}" for r in blocked) or "guardrail violation")
        self.results = results


@dataclass
class GuardrailContext:
    run_id: str
    agent: str
    extra: dict[str, Any] = field(default_factory=dict)


class GuardrailPolicy(Protocol):
    name: str
    stages: frozenset[Stage]

    def check(self, subject: Any, ctx: GuardrailContext, stage: Stage) -> GuardrailResult: ...


Recorder = Callable[[GuardrailContext, GuardrailResult], None]


class GuardrailPipeline:
    def __init__(self, policies: list[GuardrailPolicy] | None = None, recorder: Recorder | None = None) -> None:
        self.policies: list[GuardrailPolicy] = list(policies or [])
        self.recorder = recorder

    def register(self, policy: GuardrailPolicy) -> None:
        self.policies.append(policy)

    def run(self, stage: Stage, subject: Any, ctx: GuardrailContext, *, only: set[str] | None = None,
            raise_on_block: bool = True) -> list[GuardrailResult]:
        results: list[GuardrailResult] = []
        for policy in self.policies:
            if stage not in policy.stages or (only is not None and policy.name not in only):
                continue
            with get_tracer().span(SpanKind.GUARDRAIL, f"{stage.value}:{policy.name}") as span:
                try:
                    res = policy.check(subject, ctx, stage)
                except GuardrailViolation:
                    raise
                except Exception as exc:  # a crashing policy is a failed check, never a pass
                    res = GuardrailResult(stage=stage.value, policy=policy.name, passed=False, severity="block",
                                          reason=f"policy error: {type(exc).__name__}: {exc}")
                span.set(passed=res.passed, severity=res.severity)
                if not res.passed:
                    span.fail(res.reason)
            results.append(res)
            log_event(Event.GUARDRAIL_PASSED if res.passed else Event.GUARDRAIL_BLOCKED, res.reason[:300],
                      stage=res.stage, policy=res.policy, severity=res.severity)
            if self.recorder is not None and (not res.passed or res.severity != "info"):
                self.recorder(ctx, res)
        if raise_on_block and any(not r.passed and r.severity == "block" for r in results):
            raise GuardrailViolation(results)
        return results


def ok(stage: Stage, policy: str, reason: str = "passed", subject_ref: str | None = None) -> GuardrailResult:
    return GuardrailResult(stage=stage.value, policy=policy, passed=True, severity="info", reason=reason,
                           subject_ref=subject_ref)


def block(stage: Stage, policy: str, reason: str, subject_ref: str | None = None) -> GuardrailResult:
    return GuardrailResult(stage=stage.value, policy=policy, passed=False, severity="block", reason=reason,
                           subject_ref=subject_ref)


def warn(stage: Stage, policy: str, reason: str, subject_ref: str | None = None) -> GuardrailResult:
    return GuardrailResult(stage=stage.value, policy=policy, passed=True, severity="warn", reason=reason,
                           subject_ref=subject_ref)
