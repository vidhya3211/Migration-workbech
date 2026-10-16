"""Shared primitives for every typed contract in the AI Workbench."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class WorkbenchModel(BaseModel):
    """Base for all contracts: unknown fields are rejected, never silently dropped."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False, validate_assignment=True)


class Tier(StrEnum):
    T1 = "T1"  # evidence gathering / recommendation only - a human writes the answer
    T2 = "T2"  # complete proposal - requires named human approval
    T3 = "T3"  # deterministic / mechanical only - may run automatically


class SystemRole(StrEnum):
    SOURCE = "source"
    TARGET = "target"


class ReasoningMode(StrEnum):
    DETERMINISTIC = "deterministic"
    HEURISTIC = "heuristic"
    ATS = "ats"
    LLM = "llm"
    NONE = "none"


class HumanRole(StrEnum):
    ACTUARY_SME = "actuary_sme"
    DATA_STEWARD = "data_steward"
    RECONCILIATION_ANALYST = "reconciliation_analyst"
    CONDUCT_OFFICER = "conduct_officer"
    MIGRATION_LEAD = "migration_lead"


class ItemStatus(StrEnum):
    CANDIDATE = "candidate"            # produced by an agent, not yet guarded
    PROPOSED = "proposed"              # passed the deterministic guard; awaiting human review
    PENDING_REVIEW = "pending_review"  # a human task is open
    CERTIFIED = "certified"            # a named human approved it - only humans set this
    REJECTED = "rejected"              # a named human rejected it
    UNRESOLVED = "unresolved"          # no valid human decision; never treated as approval
    AUTOMATIC = "automatic"            # T3 mechanical output (spot-checked)


class SystemRef(WorkbenchModel):
    system_name: str
    system_role: SystemRole

    @field_validator("system_name")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("system_name must be supplied with every upload; it is never hardcoded")
        return v.strip()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16].upper()}"


def stable_id(prefix: str, *parts: object) -> str:
    """Deterministic id: the same inputs always give the same id (idempotent re-runs, resumable nodes)."""
    key = "|".join(str(p) for p in parts)
    return f"{prefix}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16].upper()}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def value_ref(value: object) -> str:
    """Reference for a (possibly sensitive) value: safe for logs, traces and telemetry."""
    return "sha256:" + hashlib.sha256(repr(value).encode("utf-8")).hexdigest()[:24]
