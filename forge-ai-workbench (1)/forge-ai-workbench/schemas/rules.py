"""Rule contracts shared by every extractor, the Normaliser and the Differential agent."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from schemas.common import WorkbenchModel, ItemStatus, ReasoningMode, SystemRef, Tier
from schemas.provenance import Evidence


class RuleKind(StrEnum):
    CALCULATION = "calculation"
    ASSIGNMENT = "assignment"
    CONSTANT = "constant"
    CONDITION_NAME = "condition_name"
    CONSTRAINT = "constraint"      # schema / structural rules
    SCHEDULE = "schedule"          # JCL job-control rules
    ASSERTION = "assertion"        # claims made in documents
    TABLE = "table"                # rate / lookup tables lifted from code (entries in ``context``)


class CandidateRule(WorkbenchModel):
    """The single output shape every parser agent hands the Normaliser (§33)."""

    rule_id: str
    run_id: str
    artefact_id: str
    agent: str
    tier: Tier
    kind: RuleKind
    name: str
    canonical_name: str
    expression: str
    conditions: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    evidence: Evidence
    anchor: str                                   # token that must appear in the evidence (guard-checked)
    confidence: float = Field(ge=0, le=1)
    confidence_reasons: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    context: dict = Field(default_factory=dict)
    system: SystemRef
    language: str | None = None
    status: ItemStatus = ItemStatus.CANDIDATE
    requires_human: bool = True
    reasoning_mode: ReasoningMode = ReasoningMode.DETERMINISTIC
    model: str | None = None
    prompt_version: str | None = None


class NormalisedRule(WorkbenchModel):
    rule_id: str
    canonical_name: str
    domain: str
    kind: RuleKind
    system: SystemRef
    source_kind: str                    # code | document | spreadsheet | schema | jcl
    expression: str                     # original expression
    resolved_expression: str            # canonical identifiers, constants resolved where provable
    constant_value: str | None = None   # Decimal as string when the rule is a numeric constant
    variables: list[str] = Field(default_factory=list)
    condition_signature: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    notes: list[str] = Field(default_factory=list)
    evidence: Evidence
    agent: str


class CertifiedRule(WorkbenchModel):
    rule_id: str
    run_id: str
    canonical_name: str
    domain: str
    certified_expression: str
    certified_by: str
    certified_role: str
    certified_at: datetime
    differential_id: str | None = None
    basis: str  # accept_source | accept_target | override | agreed
    evidence: list[Evidence]
