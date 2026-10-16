"""Audit event (§27): every write creates one. No exceptions."""
from __future__ import annotations

from datetime import datetime

from pydantic import Field

from schemas.common import WorkbenchModel, utc_now


class AuditEvent(WorkbenchModel):
    audit_id: str
    run_id: str
    who: str                      # agent name or named human
    actor_type: str               # agent | human | system
    what: str                     # action verb: propose | certify | reject | classify | ...
    entity_type: str
    entity_id: str
    when: datetime = Field(default_factory=utc_now)
    before: dict | None = None
    after: dict | None = None
    agent: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    tool_version: str | None = None
    reason: str = ""
    trace_id: str | None = None
