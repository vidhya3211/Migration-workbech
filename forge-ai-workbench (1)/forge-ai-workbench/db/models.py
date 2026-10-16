"""Knowledge-core ORM. PostgreSQL only (Azure Database for PostgreSQL in Azure).

Certified structured truth lives here, scoped by project -> run. Raw artefacts live in the object store
(Azure Blob Storage) and are referenced by ``blob_uri``; data extracts are staged row-by-row in
``extract_row`` so agents stream them; search indexes are derived from this schema and are never the truth.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, BigInteger, Boolean, Computed, DateTime, Float, ForeignKey, Identity, Index,
                        Integer, PrimaryKeyConstraint, String, Text)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    type_annotation_map = {dict: JsonType, list: JsonType}


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True))


class Project(Base):
    """A migration project: one source estate -> target platform assessment, with its own configuration."""
    __tablename__ = "project"
    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    business_domain: Mapped[str] = mapped_column(String(64))
    source_technologies: Mapped[list] = mapped_column(default=list)
    target_technologies: Mapped[list] = mapped_column(default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active")       # active | archived
    settings_overrides: Mapped[dict] = mapped_column(default=dict)           # PROJECT_OVERRIDABLE sections only
    pipeline: Mapped[dict] = mapped_column(default=dict)                     # {"name": ..., "disabled_steps": [...]}
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class IngestRun(Base):
    __tablename__ = "ingest_run"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.project_id"), index=True)
    pipeline_name: Mapped[str] = mapped_column(String(64), default="default")
    pipeline: Mapped[dict] = mapped_column(default=dict)                     # snapshot the run's graph is built from
    pipeline_hash: Mapped[str] = mapped_column(String(64), default="")
    settings_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    steps_total: Mapped[int] = mapped_column(Integer, default=0)
    steps_completed: Mapped[list] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(32))          # created|running|awaiting_human|completed|halted|failed|budget_stopped
    current_gate: Mapped[str | None] = mapped_column(String(64))
    source_system: Mapped[str | None] = mapped_column(String(200))
    target_system: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    halted_reason: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict] = mapped_column(default=dict)
    ledger: Mapped[dict] = mapped_column(default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(64))


class Upload(Base):
    """A bundle received for a project: receiving (chunks) -> validating -> ready | rejected; consumed by a run."""
    __tablename__ = "upload"
    upload_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.project_id"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    system_name: Mapped[str] = mapped_column(String(200))
    system_role: Mapped[str] = mapped_column(String(16))
    filename: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), default="receiving")
    declared_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    chunk_bytes: Mapped[int] = mapped_column(Integer, default=0)
    chunks_expected: Mapped[int] = mapped_column(Integer, default=0)
    bundle_sha256: Mapped[str] = mapped_column(String(64), default="")
    bundle_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    uncompressed_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    manifest: Mapped[dict] = mapped_column(default=dict)                     # summary only; files in upload_file
    stored_uri: Mapped[str] = mapped_column(String(1000), default="")
    error: Mapped[str | None] = mapped_column(Text)
    received_by: Mapped[str] = mapped_column(String(200))
    received_at: Mapped[datetime] = _ts()
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UploadFile(Base):
    __tablename__ = "upload_file"
    upload_id: Mapped[str] = mapped_column(ForeignKey("upload.upload_id", ondelete="CASCADE"), primary_key=True)
    path: Mapped[str] = mapped_column(String(1000), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))


class Artefact(Base):
    __tablename__ = "artefact"
    artefact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ingest_run.run_id"), index=True)
    upload_id: Mapped[str] = mapped_column(String(64))
    system_name: Mapped[str] = mapped_column(String(200))
    system_role: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(1000))
    artefact_type: Mapped[str] = mapped_column(String(64))
    language: Mapped[str | None] = mapped_column(String(32))
    encoding: Mapped[str | None] = mapped_column(String(32))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    line_count: Mapped[int] = mapped_column(BigInteger)
    confidence: Mapped[float] = mapped_column(Float)
    reasons: Mapped[list] = mapped_column(default=list)
    route: Mapped[str | None] = mapped_column(String(64))
    blob_uri: Mapped[str] = mapped_column(String(1000))
    classification: Mapped[str] = mapped_column(String(32))
    analysis_status: Mapped[str] = mapped_column(String(32), default="pending")
    analysis: Mapped[dict] = mapped_column(default=dict)
    ingest: Mapped[dict] = mapped_column(default=dict)


class Rule(Base):
    __tablename__ = "rule"
    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ingest_run.run_id"), index=True)
    artefact_id: Mapped[str] = mapped_column(ForeignKey("artefact.artefact_id"))
    agent: Mapped[str] = mapped_column(String(64))
    tier: Mapped[str] = mapped_column(String(4))
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(500))
    canonical_name: Mapped[str] = mapped_column(String(200), index=True)
    domain: Mapped[str | None] = mapped_column(String(64))
    expression: Mapped[str] = mapped_column(Text)
    conditions: Mapped[list] = mapped_column(default=list)
    inputs: Mapped[list] = mapped_column(default=list)
    confidence: Mapped[float] = mapped_column(Float)
    confidence_reasons: Mapped[list] = mapped_column(default=list)
    notes: Mapped[list] = mapped_column(default=list)
    context: Mapped[dict] = mapped_column(default=dict)
    system_name: Mapped[str] = mapped_column(String(200))
    system_role: Mapped[str] = mapped_column(String(16))
    language: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    requires_human: Mapped[bool] = mapped_column(Boolean)
    reasoning_mode: Mapped[str] = mapped_column(String(32))
    certified_expression: Mapped[str | None] = mapped_column(Text)
    certified_by: Mapped[str | None] = mapped_column(String(200))
    certified_role: Mapped[str | None] = mapped_column(String(64))
    certified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts()


class Differential(Base):
    __tablename__ = "differential"
    differential_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ingest_run.run_id"), index=True)
    canonical_name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(64))
    difference_type: Mapped[str] = mapped_column(String(32))
    severity_score: Mapped[float] = mapped_column(Float)
    financial_exposure: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), index=True)
    required_role: Mapped[str] = mapped_column(String(64))
    body: Mapped[dict] = mapped_column(default=dict)     # full typed Differential
    brief: Mapped[dict] = mapped_column(default=dict)    # AdjudicationBrief
    created_at: Mapped[datetime] = _ts()


class Adjudication(Base):
    __tablename__ = "adjudication"
    adjudication_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ingest_run.run_id"), index=True)
    differential_id: Mapped[str] = mapped_column(ForeignKey("differential.differential_id"))
    decision: Mapped[str] = mapped_column(String(32))
    decided_by: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(64))
    rationale: Mapped[str] = mapped_column(Text)
    certified_expression: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = _ts()


class Mapping(Base):
    __tablename__ = "mapping"
    mapping_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ingest_run.run_id"), index=True)
    source_system: Mapped[str] = mapped_column(String(200))
    source_entity: Mapped[str] = mapped_column(String(200))
    source_field: Mapped[str] = mapped_column(String(200))
    target_system: Mapped[str] = mapped_column(String(200))
    target_entity: Mapped[str] = mapped_column(String(200))
    target_field: Mapped[str] = mapped_column(String(200))
    transformation: Mapped[dict] = mapped_column(default=dict)
    score: Mapped[float] = mapped_column(Float)
    reasoning_mode: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    body: Mapped[dict] = mapped_column(default=dict)
    validation: Mapped[dict] = mapped_column(default=dict)
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts()


class Cohort(Base):
    __tablename__ = "cohort"
    cohort_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ingest_run.run_id"), index=True)
    entity: Mapped[str] = mapped_column(String(200))
    column: Mapped[str | None] = mapped_column(String(200))
    defect_code: Mapped[str] = mapped_column(String(64))
    record_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    body: Mapped[dict] = mapped_column(default=dict)
    root_cause: Mapped[dict] = mapped_column(default=dict)
    cleansing: Mapped[dict] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _ts()


class EntityMatch(Base):
    __tablename__ = "entity_match"
    match_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ingest_run.run_id"), index=True)
    left_ref: Mapped[str] = mapped_column(String(200))
    right_ref: Mapped[str] = mapped_column(String(200))
    score: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32))
    body: Mapped[dict] = mapped_column(default=dict)
    decided_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _ts()


class ReconciliationBreak(Base):
    __tablename__ = "reconciliation_break"
    break_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ingest_run.run_id"), index=True)
    record_key: Mapped[str] = mapped_column(String(200))
    entity: Mapped[str] = mapped_column(String(200))
    field: Mapped[str] = mapped_column(String(200))
    break_type: Mapped[str] = mapped_column(String(32))
    source_value: Mapped[str | None] = mapped_column(Text)
    expected_value: Mapped[str | None] = mapped_column(Text)
    target_value: Mapped[str | None] = mapped_column(Text)
    delta: Mapped[str | None] = mapped_column(String(64))
    mapping_id: Mapped[str | None] = mapped_column(String(64))
    cluster_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32))
    lineage: Mapped[dict] = mapped_column(default=dict)
    body: Mapped[dict] = mapped_column(default=dict)                         # full typed break (evidence refs)
    decided_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (Index("ix_break_run_record", "run_id", "record_key"),
                      Index("ix_break_run_signature", "run_id", "entity", "field", "break_type"))


class BreakCluster(Base):
    __tablename__ = "break_cluster"
    cluster_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ingest_run.run_id"), index=True)
    signature: Mapped[str] = mapped_column(String(500))
    field: Mapped[str] = mapped_column(String(200))
    count: Mapped[int] = mapped_column(Integer)
    total_delta: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    body: Mapped[dict] = mapped_column(default=dict)
    decided_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _ts()


class ConductCase(Base):
    __tablename__ = "conduct_case"
    case_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ingest_run.run_id"), index=True)
    record_key: Mapped[str] = mapped_column(String(200))
    impact_type: Mapped[str] = mapped_column(String(64))
    detriment: Mapped[str] = mapped_column(String(64))
    band: Mapped[str] = mapped_column(String(32))
    route: Mapped[str] = mapped_column(String(64))
    priority: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    body: Mapped[dict] = mapped_column(default=dict)
    decision: Mapped[str | None] = mapped_column(String(64))
    decided_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _ts()


class Story(Base):
    __tablename__ = "story"
    story_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ingest_run.run_id"), index=True)
    rule_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    body: Mapped[dict] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _ts()


class GeneratedCode(Base):
    __tablename__ = "generated_code"
    code_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ingest_run.run_id"), index=True)
    module_name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32))
    body: Mapped[dict] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _ts()


class Provenance(Base):
    __tablename__ = "provenance"
    provenance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(64))
    artefact_id: Mapped[str] = mapped_column(String(64))
    locator: Mapped[dict] = mapped_column(default=dict)
    excerpt: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str] = mapped_column(String(64))
    system_name: Mapped[str] = mapped_column(String(200))
    system_role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (Index("ix_provenance_entity", "entity_type", "entity_id"),)


class Audit(Base):
    __tablename__ = "audit"
    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    who: Mapped[str] = mapped_column(String(200))
    actor_type: Mapped[str] = mapped_column(String(16))
    what: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(64))
    when: Mapped[datetime] = _ts()
    before: Mapped[dict | None] = mapped_column(JsonType)
    after: Mapped[dict | None] = mapped_column(JsonType)
    agent: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    tool_version: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)


class Signal(Base):
    __tablename__ = "signal"
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(64), unique=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    agent: Mapped[str] = mapped_column(String(64))
    tier: Mapped[str] = mapped_column(String(4))
    signal_type: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    item_refs: Mapped[list] = mapped_column(default=list)
    data: Mapped[dict] = mapped_column(default=dict)
    artefact_id: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (Index("ix_signal_run_seq", "run_id", "seq"),)


class HitlTask(Base):
    __tablename__ = "hitl_task"
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    gate: Mapped[str] = mapped_column(String(64))
    item_type: Mapped[str] = mapped_column(String(64))
    item_id: Mapped[str] = mapped_column(String(64), index=True)
    required_role: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    brief: Mapped[dict] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(32))
    decision: Mapped[dict | None] = mapped_column(JsonType)
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (Index("ix_hitl_run_status_gate", "run_id", "status", "gate"),)


class AgentExecution(Base):
    __tablename__ = "agent_execution"
    execution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_name: Mapped[str] = mapped_column(String(64))
    tier: Mapped[str] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(32))
    duration_ms: Mapped[float] = mapped_column(Float)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = _ts()
    record: Mapped[dict] = mapped_column(default=dict)


class TraceSpan(Base):
    __tablename__ = "trace_span"
    span_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(32))
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(64))
    agent_tier: Mapped[str | None] = mapped_column(String(4))
    kind: Mapped[str] = mapped_column(String(32))
    operation: Mapped[str] = mapped_column(String(200))
    start_time: Mapped[datetime] = _ts()
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)
    input_reference: Mapped[str | None] = mapped_column(String(200))
    output_reference: Mapped[str | None] = mapped_column(String(200))
    attributes: Mapped[dict] = mapped_column(default=dict)


class LLMCall(Base):
    __tablename__ = "llm_call"
    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    agent: Mapped[str] = mapped_column(String(64))
    record: Mapped[dict] = mapped_column(default=dict)
    estimated_cost: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = _ts()


class HeadroomEvent(Base):
    __tablename__ = "headroom_event"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    agent: Mapped[str] = mapped_column(String(64))
    record: Mapped[dict] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _ts()


class GuardrailEvent(Base):
    __tablename__ = "guardrail_event"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    agent: Mapped[str | None] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(64))
    policy: Mapped[str] = mapped_column(String(64))
    passed: Mapped[bool] = mapped_column(Boolean)
    severity: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts()


class MCPInvocation(Base):
    __tablename__ = "mcp_invocation"
    invocation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    agent: Mapped[str] = mapped_column(String(64))
    server: Mapped[str] = mapped_column(String(64))
    tool: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32))
    record: Mapped[dict] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _ts()


class AssuranceReport(Base):
    __tablename__ = "assurance_report"
    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    passed: Mapped[bool] = mapped_column(Boolean)
    readiness_score: Mapped[float] = mapped_column(Float)
    body: Mapped[dict] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _ts()


class DataDictionaryEntry(Base):
    __tablename__ = "data_dictionary"
    entry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    system_name: Mapped[str] = mapped_column(String(200))
    system_role: Mapped[str] = mapped_column(String(16))
    entity: Mapped[str] = mapped_column(String(200))
    field: Mapped[str] = mapped_column(String(200))
    body: Mapped[dict] = mapped_column(default=dict)


class EvalResult(Base):
    __tablename__ = "eval_result"
    eval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    capability: Mapped[str] = mapped_column(String(64))
    passed: Mapped[bool] = mapped_column(Boolean)
    metrics: Mapped[dict] = mapped_column(default=dict)
    config_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts()


class Job(Base):
    """Durable background work (upload validation, run start, resume). Claimed with FOR UPDATE SKIP LOCKED;
    with the Service Bus backend the queue carries only the job id and this row stays the source of truth."""
    __tablename__ = "job"
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    upload_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16))                           # queued|running|succeeded|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = _ts()
    locked_by: Mapped[str | None] = mapped_column(String(200))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(default=dict)
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    __table_args__ = (Index("ix_job_claim", "status", "available_at"),)


class ExtractTable(Base):
    """A data extract staged into PostgreSQL (delimited, or fixed-width EBCDIC decoded through its copybook)."""
    __tablename__ = "extract_table"
    artefact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ingest_run.run_id"), index=True)
    system_name: Mapped[str] = mapped_column(String(200))
    system_role: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(1000))
    format: Mapped[str] = mapped_column(String(32))                           # delimited | fixed_width
    delimiter: Mapped[str | None] = mapped_column(String(4))
    columns: Mapped[list] = mapped_column(default=list)
    row_count: Mapped[int] = mapped_column(BigInteger, default=0)
    first_line: Mapped[int] = mapped_column(BigInteger, default=2)            # physical line of the first data row
    controls: Mapped[dict] = mapped_column(default=dict)                      # trailer control totals, rejects
    staged_at: Mapped[datetime] = _ts()


class ExtractRow(Base):
    """One staged data row; ``vals`` is a JSON array aligned with ``extract_table.columns``."""
    __tablename__ = "extract_row"
    artefact_id: Mapped[str] = mapped_column(String(64))
    line_no: Mapped[int] = mapped_column(BigInteger)
    vals: Mapped[list] = mapped_column()
    __table_args__ = (PrimaryKeyConstraint("artefact_id", "line_no", name="pk_extract_row"),)


class ReconKey(Base):
    """Working table for reconciliation: source records' *transformed* keys, joined to the target in SQL."""
    __tablename__ = "recon_key"
    run_id: Mapped[str] = mapped_column(String(64))
    pair_id: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(Text)
    line_no: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (PrimaryKeyConstraint("run_id", "pair_id", "line_no", name="pk_recon_key"),
                      Index("ix_recon_key_lookup", "run_id", "pair_id", "key"),
                      {"prefixes": ["UNLOGGED"]})


class ConductOutcomeRow(Base):
    """Customer outcomes flagged by the Outcome agent, consumed by Case routing (working data, not certified)."""
    __tablename__ = "conduct_outcome"
    case_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    record_key: Mapped[str] = mapped_column(String(200))
    impact_type: Mapped[str] = mapped_column(String(64))
    body: Mapped[dict] = mapped_column(default=dict)


class ColumnProfile(Base):
    __tablename__ = "column_profile"
    profile_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str] = mapped_column(String(200))
    column: Mapped[str] = mapped_column(String(200))
    system_name: Mapped[str] = mapped_column(String(200))
    body: Mapped[dict] = mapped_column(default=dict)


class LiveArchiveScore(Base):
    __tablename__ = "live_archive_score"
    score_id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64))
    artefact_id: Mapped[str] = mapped_column(String(64))
    record_ref: Mapped[str] = mapped_column(String(200))
    live_probability: Mapped[float] = mapped_column(Float)
    recommendation: Mapped[str] = mapped_column(String(32))
    components: Mapped[dict] = mapped_column(default=dict)
    __table_args__ = (Index("ix_live_archive_run_rec", "run_id", "recommendation"),)


class RetrievalDocument(Base):
    """Search index over the *certified* core (PostgreSQL full-text; pgvector embeddings live in
    ``retrieval_embedding`` when the extension is installed). A hit is a pointer, never the truth."""
    __tablename__ = "retrieval_document"
    doc_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    ref: Mapped[str] = mapped_column(String(200))
    doc_metadata: Mapped[dict] = mapped_column("metadata", default=dict)
    search_vector = mapped_column(TSVECTOR, Computed("to_tsvector('english', coalesce(title, '') || ' ' || "
                                                     "coalesce(content, ''))", persisted=True))
    __table_args__ = (Index("ix_retrieval_fts", "search_vector", postgresql_using="gin"),)


class LoginAttempt(Base):
    __tablename__ = "login_attempt"
    username: Mapped[str] = mapped_column(String(200), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


GOVERNED_ENTITIES = {"rule", "mapping", "cohort", "reconciliation_break", "conduct_case"}
