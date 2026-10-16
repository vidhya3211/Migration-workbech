"""Correlation context shared by traces, logs, audit and telemetry (§17-18).

One chain: Run -> Graph -> Agent -> Tool -> MCP -> Headroom -> LLM -> Guardrail -> Human -> Database.
Values live in contextvars, so LangGraph's parallel ``Send`` branches each carry their own copy.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

CORRELATION_KEYS = (
    "trace_id", "span_id", "project_id", "run_id", "agent_id", "agent_tier", "signal_id", "artefact_id", "system_name",
    "system_role", "job_id",
)

_vars: dict[str, ContextVar[str | None]] = {k: ContextVar(f"workbench_{k}", default=None) for k in CORRELATION_KEYS}


def get(key: str) -> str | None:
    return _vars[key].get()


def correlation() -> dict[str, str]:
    return {k: v for k in CORRELATION_KEYS if (v := _vars[k].get()) is not None}


@contextmanager
def bind(**values: str | None) -> Iterator[None]:
    tokens = []
    for key, value in values.items():
        if key not in _vars:
            raise KeyError(f"unknown correlation key {key!r}")
        tokens.append((_vars[key], _vars[key].set(value)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
