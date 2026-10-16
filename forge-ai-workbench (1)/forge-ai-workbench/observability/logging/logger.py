"""Structured JSON logging with correlation fields (§17).

Sensitive payloads are never logged by default: any field not on the scalar allow-list, or named like a
payload, is replaced by a hash reference unless ``observability.log_payloads`` is explicitly enabled
(and even then PII-classified fields stay hashed).
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from observability import context
from schemas.common import value_ref


class Event(StrEnum):
    RUN_STARTED = "RUN_STARTED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_FAILED = "RUN_FAILED"
    RUN_INTERRUPTED = "RUN_INTERRUPTED"
    AGENT_STARTED = "AGENT_STARTED"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_FAILED = "AGENT_FAILED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    TOOL_FAILED = "TOOL_FAILED"
    MCP_STARTED = "MCP_STARTED"
    MCP_COMPLETED = "MCP_COMPLETED"
    MCP_FAILED = "MCP_FAILED"
    LLM_STARTED = "LLM_STARTED"
    LLM_COMPLETED = "LLM_COMPLETED"
    LLM_FAILED = "LLM_FAILED"
    HEADROOM_OPTIMIZED = "HEADROOM_OPTIMIZED"
    HEURISTIC_EVALUATED = "HEURISTIC_EVALUATED"
    ATS_STARTED = "ATS_STARTED"
    ATS_NODE_EXPANDED = "ATS_NODE_EXPANDED"
    ATS_NODE_PRUNED = "ATS_NODE_PRUNED"
    ATS_COMPLETED = "ATS_COMPLETED"
    GUARDRAIL_PASSED = "GUARDRAIL_PASSED"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"
    DETERMINISTIC_GUARD_PASSED = "DETERMINISTIC_GUARD_PASSED"
    DETERMINISTIC_GUARD_FAILED = "DETERMINISTIC_GUARD_FAILED"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    HUMAN_DECISION_RECEIVED = "HUMAN_DECISION_RECEIVED"
    PROVENANCE_WRITTEN = "PROVENANCE_WRITTEN"
    AUDIT_WRITTEN = "AUDIT_WRITTEN"
    KNOWLEDGE_CORE_WRITE = "KNOWLEDGE_CORE_WRITE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


_SAFE_SCALARS = (str, int, float, bool, type(None))
_PAYLOAD_HINTS = ("payload", "content", "prompt", "excerpt", "value", "text", "raw", "row", "record", "messages")
_log_payloads = False
_logger = logging.getLogger("ai_workbench")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "workbench_event", None),
            "message": record.getMessage(),
        }
        doc.update(getattr(record, "workbench_correlation", {}))
        doc.update(getattr(record, "workbench_fields", {}))
        if record.exc_info:
            doc["exception"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str)


def configure_logging(level: str = "INFO", log_payloads: bool = False, stream: Any = None) -> None:
    global _log_payloads
    _log_payloads = log_payloads
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    _logger.handlers[:] = [handler]
    _logger.setLevel(level)
    _logger.propagate = False
    for noisy in ("headroom", "httpx", "mcp"):   # third-party INFO chatter stays out of the structured stream
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _sanitise(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        lowered = key.lower()
        payload_like = any(h in lowered for h in _PAYLOAD_HINTS)
        if payload_like and not _log_payloads:
            out[key + "_ref"] = value_ref(value)
        elif isinstance(value, _SAFE_SCALARS):
            out[key] = value
        elif isinstance(value, (list, tuple)) and all(isinstance(v, (str, int, float)) for v in value) and len(value) <= 50:
            out[key] = list(value)
        else:
            out[key + "_ref"] = value_ref(value)
    return out


def log_event(event: Event, message: str = "", level: int = logging.INFO, **fields: Any) -> None:
    _logger.log(level, message or event.value, extra={
        "workbench_event": event.value,
        "workbench_correlation": context.correlation(),
        "workbench_fields": _sanitise(fields),
    })


def get_logger() -> logging.Logger:
    return _logger
