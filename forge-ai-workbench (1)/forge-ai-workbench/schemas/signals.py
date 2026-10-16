"""Signals: every message an agent posts to the blackboard (§25). Powers the live console view."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from schemas.common import WorkbenchModel, Tier, utc_now


class SignalType(StrEnum):
    ORCHESTRATION = "orchestration"
    INGEST_MANIFEST = "ingest_manifest"
    ARTEFACTS_CLASSIFIED = "artefacts_classified"
    CANDIDATE_RULES = "candidate_rules"
    DATA_DICTIONARY = "data_dictionary"
    NORMALISED_RULES = "normalised_rules"
    DIFFERENTIALS = "differentials"
    ADJUDICATION_BRIEFS = "adjudication_briefs"
    CERTIFIED_RULES = "certified_rules"
    USER_STORIES = "user_stories"
    MAPPING_PROPOSALS = "mapping_proposals"
    MAPPING_VALIDATION = "mapping_validation"
    GENERATED_CODE = "generated_code"
    COLUMN_PROFILES = "column_profiles"
    DQ_COHORTS = "dq_cohorts"
    ROOT_CAUSES = "root_causes"
    CLEANSING_PROPOSALS = "cleansing_proposals"
    LIVE_ARCHIVE_SCORES = "live_archive_scores"
    ENTITY_MATCHES = "entity_matches"
    RECON_BREAKS = "recon_breaks"
    BREAK_CLUSTERS = "break_clusters"
    LINEAGE = "lineage"
    CONDUCT_OUTCOMES = "conduct_outcomes"
    CASE_ROUTES = "case_routes"
    RETRIEVAL_INDEX = "retrieval_index"
    ASSISTANT_ANSWER = "assistant_answer"
    ASSURANCE_REPORT = "assurance_report"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    GUARDRAIL = "guardrail"


class SignalLevel(StrEnum):
    INFO = "info"
    WARN = "warn"
    REFUSAL = "refusal"
    ERROR = "error"


class Signal(WorkbenchModel):
    signal_id: str
    run_id: str
    agent: str
    tier: Tier
    signal_type: SignalType
    level: SignalLevel = SignalLevel.INFO
    message: str
    item_refs: list[str] = Field(default_factory=list)   # ids of typed items written to the blackboard
    data: dict = Field(default_factory=dict)             # small, non-sensitive summary only
    artefact_id: str | None = None
    trace_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
