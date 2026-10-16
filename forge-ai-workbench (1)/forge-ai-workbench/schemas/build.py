"""Stories, assistant answers, assurance reports and HITL tasks."""
from __future__ import annotations

from datetime import datetime

from pydantic import Field

from schemas.common import WorkbenchModel, HumanRole, ItemStatus, utc_now
from schemas.provenance import Evidence


class UserStory(WorkbenchModel):
    story_id: str
    run_id: str
    rule_id: str
    title: str
    narrative: str
    acceptance_criteria: list[str]
    test_vectors: list[dict] = Field(default_factory=list)
    evidence: list[Evidence]
    status: ItemStatus = ItemStatus.PROPOSED
    model_assisted: dict | None = None      # {model, prompt_version, guard} when a model drafted the prose


class Citation(WorkbenchModel):
    ref: str                       # rule / differential / provenance id
    label: str
    excerpt: str


class AssistantAnswer(WorkbenchModel):
    question: str
    answer: str
    refused: bool
    refusal_reason: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    mode: str                      # extractive | llm
    confidence: float = 0.0


class CapabilityScore(WorkbenchModel):
    capability: str
    metrics: dict[str, float]
    thresholds: dict[str, float] = Field(default_factory=dict)
    passed: bool
    notes: list[str] = Field(default_factory=list)


class AssuranceReport(WorkbenchModel):
    report_id: str
    run_id: str
    created_at: datetime = Field(default_factory=utc_now)
    capabilities: list[CapabilityScore]
    cost: dict = Field(default_factory=dict)
    latency: dict = Field(default_factory=dict)
    readiness_score: float
    passed: bool
    blockers: list[str] = Field(default_factory=list)


class HitlTask(WorkbenchModel):
    task_id: str
    run_id: str
    gate: str
    item_type: str
    item_id: str
    required_role: HumanRole
    title: str
    brief: dict = Field(default_factory=dict)
    status: str = "pending"        # pending | decided | closed_unresolved
    created_at: datetime = Field(default_factory=utc_now)
