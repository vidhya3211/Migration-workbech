"""Differentials, adjudication briefs and human decisions (Stage 2-3)."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator

from schemas.common import WorkbenchModel, HumanRole, ItemStatus, utc_now
from schemas.provenance import Evidence
from schemas.reasoning import HeuristicResult


class DifferenceType(StrEnum):
    VALUE_MISMATCH = "value_mismatch"
    EXPRESSION_MISMATCH = "expression_mismatch"
    CONDITION_MISMATCH = "condition_mismatch"
    MISSING_IN_TARGET = "missing_in_target"
    MISSING_IN_SOURCE = "missing_in_source"
    DOCUMENT_CONFLICT = "document_conflict"


class DivergenceVector(WorkbenchModel):
    """A deterministic proof of disagreement: same inputs, different outputs."""

    inputs: dict[str, str]
    source_value: str | None
    target_value: str | None


class Differential(WorkbenchModel):
    differential_id: str
    run_id: str
    canonical_name: str
    domain: str
    difference_type: DifferenceType
    source_rule_ids: list[str] = Field(default_factory=list)
    target_rule_ids: list[str] = Field(default_factory=list)
    document_rule_ids: list[str] = Field(default_factory=list)
    source_summary: str | None = None
    target_summary: str | None = None
    document_summary: str | None = None
    source_system: str | None = None
    target_system: str | None = None
    explanation: str
    explanation_mode: str = "deterministic"   # deterministic | llm
    divergence: list[DivergenceVector] = Field(default_factory=list)
    severity: HeuristicResult
    financial_exposure: str = "0"             # Decimal as string, per affected policy-year
    real_book: dict | None = None             # both rules executed over the staged source records: how many differ
    evidence: list[Evidence]
    required_role: HumanRole = HumanRole.ACTUARY_SME
    status: ItemStatus = ItemStatus.PROPOSED


class AdjudicationBrief(WorkbenchModel):
    """T1 output: briefs the right named expert. It recommends nothing and decides nothing."""

    brief_id: str
    differential_id: str
    required_role: HumanRole
    question: str
    options: list[str]
    source_position: str | None
    target_position: str | None
    document_position: str | None
    evidence: list[Evidence]
    exposure_note: str
    narrative: dict | None = None           # model-drafted brief (summary, readings, impact, questions) - advisory
    model_assisted: dict | None = None      # {model, prompt_version, guard} when a model drafted the prose


class HumanDecision(WorkbenchModel):
    """A durable decision by a *named* human. Absence of one is never approval."""

    item_id: str
    item_type: str
    decision: str
    decided_by: str
    role: HumanRole
    rationale: str = ""
    certified_expression: str | None = None
    decided_at: datetime = Field(default_factory=utc_now)

    @field_validator("decided_by")
    @classmethod
    def _named(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("a decision must name the human who made it")
        return v.strip()


class Adjudication(WorkbenchModel):
    adjudication_id: str
    run_id: str
    differential_id: str
    decision: HumanDecision
