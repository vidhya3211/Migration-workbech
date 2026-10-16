"""Reconciliation and conduct contracts (Stage 5, wave 2: source + target data)."""
from __future__ import annotations

from pydantic import Field

from schemas.common import WorkbenchModel, HumanRole, ItemStatus
from schemas.provenance import Evidence
from schemas.reasoning import HeuristicResult


class ReconciliationBreak(WorkbenchModel):
    break_id: str
    run_id: str
    record_key: str
    entity: str
    field: str                      # target field
    break_type: str                 # value_mismatch | missing_in_target | unexpected_in_target | rejected_at_load
                                    # | transform_error
    source_value: str | None
    expected_value: str | None      # source value after the certified transformation
    target_value: str | None
    delta: str | None = None        # Decimal as string for numeric fields
    mapping_id: str | None = None
    cluster_id: str | None = None
    tolerance: dict | None = None   # {absolute_floor, relative_bp} applied before this was called a break
    effective_date: str | None = None   # the date both sides were compared at (date-aligned reconciliation)
    note: str | None = None         # e.g. the load-rejection reason
    evidence: list[Evidence]
    status: ItemStatus = ItemStatus.AUTOMATIC


class BreakCluster(WorkbenchModel):
    cluster_id: str
    run_id: str
    signature: str
    field: str
    break_ids: list[str]
    count: int
    total_delta: str
    hypothesis: str
    linked_differential_id: str | None = None
    linked_rule: str | None = None
    score: HeuristicResult
    model_hypothesis: dict | None = None     # unproven model suggestion when replay could not explain the cluster
    shape: dict | None = None                # {kind: exact|shift|band, median_shift, dispersion, systematic, ...}
    required_role: HumanRole = HumanRole.RECONCILIATION_ANALYST
    status: ItemStatus = ItemStatus.PROPOSED


class LineageStep(WorkbenchModel):
    stage: str
    description: str
    reference: str


class LineageTrace(WorkbenchModel):
    break_id: str
    steps: list[LineageStep]


class ConductOutcome(WorkbenchModel):
    case_id: str
    run_id: str
    record_key: str
    break_ids: list[str]
    impact_type: str                 # overcharge | underpayment | benefit_reduction | missing_record | right_lost
    detriment: str                   # Decimal, customer detriment per period
    annualised_detriment: str
    evidence: list[Evidence]
    rationale: str
    elements: list[str] = Field(default_factory=list)   # e.g. the protected rights lost
    vulnerable: bool = False
    vulnerability_reasons: list[str] = Field(default_factory=list)


class ConductCase(WorkbenchModel):
    case_id: str
    run_id: str
    outcome: ConductOutcome
    materiality: HeuristicResult
    band: str
    route: str                       # conduct queue name
    priority: int
    required_role: HumanRole = HumanRole.CONDUCT_OFFICER
    status: ItemStatus = ItemStatus.PROPOSED
    impact_note: dict | None = None          # model-drafted note for the conduct officer - the officer authors
    duty_outcome: str | None = None          # Consumer Duty outcome the change bears on (products-services, ...)
