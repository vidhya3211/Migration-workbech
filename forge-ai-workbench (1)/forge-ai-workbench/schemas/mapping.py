"""Mapping / build contracts (Stage 5)."""
from __future__ import annotations

from pydantic import Field

from schemas.common import WorkbenchModel, ItemStatus, ReasoningMode, SystemRef
from schemas.provenance import Evidence
from schemas.reasoning import HeuristicResult


class DictionaryEntry(WorkbenchModel):
    """One attribute of a source/target data dictionary (from DDL or extract headers)."""

    entry_id: str
    system: SystemRef
    entity: str
    field: str
    data_type: str | None = None       # normalised: string | integer | decimal | date | boolean
    length: int | None = None
    scale: int | None = None
    nullable: bool = True
    primary_key: bool = False
    allowed_values: list[str] = Field(default_factory=list)
    range: tuple[str, str] | None = None     # BETWEEN a AND b constraint
    description: str | None = None
    evidence: Evidence


class TransformationSpec(WorkbenchModel):
    kind: str                           # direct | cast | code_map | date_format | expression
    params: dict = Field(default_factory=dict)
    description: str


class MappingProposal(WorkbenchModel):
    mapping_id: str
    run_id: str
    source: DictionaryEntry
    target: DictionaryEntry
    transformation: TransformationSpec
    score: float = Field(ge=0, le=1)
    heuristics: list[HeuristicResult]
    alternatives: list[str] = Field(default_factory=list)
    reasoning_mode: ReasoningMode
    reasoning_notes: list[str] = Field(default_factory=list)
    status: ItemStatus = ItemStatus.PROPOSED


class ValidationFinding(WorkbenchModel):
    mapping_id: str
    check: str
    passed: bool
    severity: str  # info | warn | block
    detail: str


class MappingValidation(WorkbenchModel):
    mapping_id: str
    verdict: str  # agree | concerns | disagree
    findings: list[ValidationFinding]


class GeneratedCode(WorkbenchModel):
    code_id: str
    run_id: str
    mapping_ids: list[str]
    module_name: str
    language: str = "python"
    source: str
    tests: str
    validation: dict = Field(default_factory=dict)   # compile / unsafe-code / tests results
    status: ItemStatus = ItemStatus.PROPOSED
