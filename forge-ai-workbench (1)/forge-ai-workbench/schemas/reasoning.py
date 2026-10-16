"""Contracts for reasoning results: heuristics are candidates/ranking, never certification."""
from __future__ import annotations

from pydantic import Field

from schemas.common import WorkbenchModel, ReasoningMode


class HeuristicResult(WorkbenchModel):
    """Every heuristic exposes how it scored - heuristic logic is never hidden inside prompts (§5)."""

    heuristic: str
    candidate: str
    score: float = Field(ge=0, le=1)
    score_components: dict[str, float]
    threshold: float
    passed: bool
    evidence: list[str] = Field(default_factory=list)
    reason: str
    algorithm_version: str


class ReasoningTrace(WorkbenchModel):
    """What the reasoning router decided and what each mechanism contributed for one decision."""

    decision_id: str
    task: str
    route: list[ReasoningMode]
    route_reasons: list[str]
    deterministic: dict = Field(default_factory=dict)
    heuristics: list[HeuristicResult] = Field(default_factory=list)
    ats_run_id: str | None = None
    llm_call_ids: list[str] = Field(default_factory=list)
    outcome: str
    confidence: float = Field(ge=0, le=1, default=0)
