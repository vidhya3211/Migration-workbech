"""MCP abstraction contracts (§31)."""
from __future__ import annotations

from datetime import datetime

from pydantic import Field

from schemas.common import WorkbenchModel, utc_now


class MCPToolPolicy(WorkbenchModel):
    allowed_agents: frozenset[str]
    data_classification: str = "internal"
    rate_limit_per_minute: int = 60
    timeout_seconds: float = 10
    requires_human_approval: bool = False


class MCPToolDefinition(WorkbenchModel):
    qualified_name: str          # "<server>.<tool>" - the only name agents use
    server: str
    remote_name: str
    description: str = ""
    input_schema: dict = Field(default_factory=dict)
    version: str = "1"
    policy: MCPToolPolicy


class MCPInvocation(WorkbenchModel):
    invocation_id: str
    correlation_id: str
    run_id: str
    agent: str
    tool: str
    arguments_ref: str
    started_at: datetime = Field(default_factory=utc_now)
    approval_ref: str | None = None


class MCPResult(WorkbenchModel):
    invocation_id: str
    tool: str
    status: str                  # ok | error | rejected | timeout | pending_approval
    text: str = ""
    structured: dict | None = None
    untrusted: bool = True       # remote output is always untrusted input
    latency_ms: float = 0
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    output_ref: str | None = None
