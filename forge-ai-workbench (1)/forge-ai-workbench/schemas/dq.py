"""Data-quality contracts (Stage 5, wave 1: source data)."""
from __future__ import annotations

from pydantic import Field

from schemas.common import WorkbenchModel, ItemStatus, SystemRef
from schemas.provenance import Evidence
from schemas.reasoning import HeuristicResult


class ColumnProfile(WorkbenchModel):
    entity: str
    column: str
    system: SystemRef
    row_count: int
    null_count: int
    distinct_count: int
    inferred_type: str
    min_value: str | None = None
    max_value: str | None = None
    top_values: list[tuple[str, int]] = Field(default_factory=list)   # empty for PII columns
    pii: bool = False
    pattern_violations: int = 0


class Cohort(WorkbenchModel):
    cohort_id: str
    run_id: str
    entity: str
    column: str | None
    defect_code: str               # null_mandatory | invalid_date | invalid_code | out_of_range | duplicate_key | sentinel_value
    description: str
    record_count: int
    record_refs: list[str]         # business-key references (pseudonymised where needed)
    anomaly: HeuristicResult
    rule_ids: list[str] = Field(default_factory=list)   # certified rules that define "correct"
    evidence: list[Evidence]
    status: ItemStatus = ItemStatus.PROPOSED


class RootCauseHypothesis(WorkbenchModel):
    cohort_id: str
    cause_code: str
    description: str
    score: float = Field(ge=0, le=1)
    evidence: list[str]
    alternatives: list[str] = Field(default_factory=list)
    ats_run_id: str | None = None


class CleansingProposal(WorkbenchModel):
    proposal_id: str
    cohort_id: str
    action: str                    # default_value | reformat | remap_code | quarantine | manual_review
    params: dict = Field(default_factory=dict)
    rationale: str
    status: ItemStatus = ItemStatus.PROPOSED
    model_assisted: dict | None = None      # {model, prompt_version, guard} when a model drafted the prose


class LiveArchiveScore(WorkbenchModel):
    record_ref: str
    live_probability: float = Field(ge=0, le=1)
    components: dict[str, float]
    recommendation: str            # likely_live | likely_archive | review


class EntityMatch(WorkbenchModel):
    match_id: str
    run_id: str
    left_ref: str
    right_ref: str
    score: HeuristicResult
    evidence: list[Evidence]
    grade: str = "ambiguous"                 # strong | ambiguous - orders the steward's queue, decides nothing
    blocked_on: list[str] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)   # fields the two records disagree on (e.g. consent)
    status: ItemStatus = ItemStatus.PROPOSED
