"""Agent execution contract (§42) and the telemetry records it aggregates."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from schemas.common import WorkbenchModel, ReasoningMode, Tier, utc_now


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"          # a guardrail stopped it - explicit, never silent
    BUDGET_STOPPED = "budget_stopped"


class LLMCallRecord(WorkbenchModel):
    call_id: str
    run_id: str
    agent: str
    purpose: str
    provider: str
    model: str
    prompt_id: str
    prompt_version: str
    input_tokens: int
    compressed_input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    estimated_cost: str          # Decimal as string
    latency_ms: float
    compression_ratio: float
    status: str                  # ok | error | refused | budget_stopped | schema_invalid
    error: str | None = None
    trace_id: str | None = None
    span_id: str | None = None


class HeadroomMetrics(WorkbenchModel):
    block_id: str
    content_type: str
    compression_mode: str        # pass_through | reversible | pass_through_fallback | disabled
    compression_policy: str
    tokens_before: int
    tokens_after: int
    compression_ratio: float     # tokens saved / tokens before
    original_reference: str | None = None
    retrieval_available: bool = False
    transforms: list[str] = Field(default_factory=list)


class GuardrailResult(WorkbenchModel):
    stage: str
    policy: str
    passed: bool
    severity: str                # info | warn | block
    reason: str
    subject_ref: str | None = None


class ToolCallRecord(WorkbenchModel):
    tool: str
    kind: str                    # native | mcp
    status: str
    latency_ms: float
    input_ref: str | None = None
    output_ref: str | None = None
    error: str | None = None
    correlation_id: str | None = None


class AgentExecution(WorkbenchModel):
    execution_id: str
    run_id: str
    agent_name: str
    tier: Tier
    input_references: list[str] = Field(default_factory=list)
    tools_used: list[ToolCallRecord] = Field(default_factory=list)
    mcp_tools_used: list[ToolCallRecord] = Field(default_factory=list)
    reasoning_mode: list[ReasoningMode] = Field(default_factory=list)
    llm_calls: list[LLMCallRecord] = Field(default_factory=list)
    heuristic_calls: int = 0
    ats_calls: list[str] = Field(default_factory=list)
    headroom_metrics: list[HeadroomMetrics] = Field(default_factory=list)
    guardrail_results: list[GuardrailResult] = Field(default_factory=list)
    output_signal: str | None = None
    output_refs: list[str] = Field(default_factory=list)
    provenance: list[str] = Field(default_factory=list)
    audit: list[str] = Field(default_factory=list)
    trace_id: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    duration_ms: float = 0
    status: ExecutionStatus = ExecutionStatus.COMPLETED
    error: str | None = None
