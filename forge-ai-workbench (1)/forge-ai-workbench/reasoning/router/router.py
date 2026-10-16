"""Central reasoning router (§8): use the minimum reasoning a decision needs.

    deterministic possible?      -> deterministic
    candidate ranking required?  -> heuristics
    multiple close alternatives? -> ATS
    semantic interpretation?     -> LLM (only if a model is available; otherwise route to a human)
    always                       -> deterministic validation + guard afterwards

Never "run everything on everything": each mechanism must be justified by a property of the task.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from observability.tracing.tracer import SpanKind, get_tracer
from schemas.common import ReasoningMode


class ReasoningTask(BaseModel):
    task: str
    deterministic_possible: bool = True
    deterministic_sufficient: bool = False     # the deterministic result is already conclusive
    candidates: int = 0
    close_alternatives: int = 0                # candidates within the ambiguity margin of the best
    needs_semantics: bool = False
    llm_available: bool = False
    tier_allows_llm: bool = True
    tier_allows_ats: bool = True


class ReasoningPlan(BaseModel):
    route: list[ReasoningMode]
    reasons: list[str] = Field(default_factory=list)
    requires_human_interpretation: bool = False


def route(task: ReasoningTask) -> ReasoningPlan:
    plan = ReasoningPlan(route=[])
    with get_tracer().span(SpanKind.REASONING, f"route:{task.task}") as span:
        if task.deterministic_possible:
            plan.route.append(ReasoningMode.DETERMINISTIC)
            plan.reasons.append("deterministic analysis is possible - always first")
        if task.deterministic_sufficient:
            plan.reasons.append("deterministic result is conclusive - no further reasoning")
        else:
            if task.candidates > 1:
                plan.route.append(ReasoningMode.HEURISTIC)
                plan.reasons.append(f"{task.candidates} candidates need ranking")
            if task.close_alternatives > 1:
                if task.tier_allows_ats:
                    plan.route.append(ReasoningMode.ATS)
                    plan.reasons.append(f"{task.close_alternatives} close alternatives - bounded exploration")
                else:
                    plan.reasons.append("alternatives exist but tier forbids ATS - ranked list goes to a human")
            if task.needs_semantics:
                if task.llm_available and task.tier_allows_llm:
                    plan.route.append(ReasoningMode.LLM)
                    plan.reasons.append("semantic interpretation required")
                else:
                    plan.requires_human_interpretation = True
                    plan.reasons.append("semantic interpretation required but no LLM is permitted/available - "
                                        "routed to a human with the evidence")
        plan.reasons.append("result is validated deterministically and guarded before it can be used")
        span.set(route=",".join(m.value for m in plan.route), human=plan.requires_human_interpretation)
    return plan
