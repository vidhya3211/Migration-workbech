"""Agent tracing (§16). Every agent run produces a trace; every span carries the mandatory fields.

Spans are written to pluggable sinks: the knowledge-core ``trace_span`` table (console drill-down),
an in-memory sink (tests/evals) and, when enabled, OpenTelemetry -> Azure Monitor / Application Insights.
Span attributes are references and counts - never raw customer data.
"""
from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterator, Protocol

from pydantic import BaseModel, Field

from observability import context


class SpanKind(StrEnum):
    RUN = "run"
    GRAPH = "graph"
    AGENT = "agent"
    REASONING = "reasoning"
    LLM = "llm"
    TOOL = "tool"
    MCP = "mcp"
    HEURISTIC = "heuristic"
    ATS = "ats"
    HEADROOM = "headroom"
    GUARDRAIL = "guardrail"
    HUMAN = "human"
    DATABASE = "database"


class Span(BaseModel):
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    run_id: str | None = None
    agent_id: str | None = None
    agent_tier: str | None = None
    kind: SpanKind
    operation: str
    start_time: datetime
    end_time: datetime | None = None
    duration_ms: float | None = None
    status: str = "ok"
    error: str | None = None
    input_reference: str | None = None
    output_reference: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class SpanSink(Protocol):
    def export(self, span: Span) -> None: ...


class InMemorySink:
    def __init__(self) -> None:
        self.spans: list[Span] = []
        self._lock = threading.Lock()

    def export(self, span: Span) -> None:
        with self._lock:
            self.spans.append(span)


class SpanHandle:
    def __init__(self, span: Span) -> None:
        self.span = span

    def set(self, **attributes: Any) -> None:
        for key, value in attributes.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                self.span.attributes[key] = value
            else:
                self.span.attributes[key] = str(value)[:200]

    def output(self, reference: str | None) -> None:
        self.span.output_reference = reference

    def fail(self, error: str) -> None:
        self.span.status = "error"
        self.span.error = error[:1000]


class Tracer:
    def __init__(self) -> None:
        self._sinks: list[SpanSink] = []
        self._lock = threading.Lock()
        self._otel = None

    def add_sink(self, sink: SpanSink) -> None:
        with self._lock:
            self._sinks.append(sink)

    def remove_sink(self, sink: SpanSink) -> None:
        with self._lock:
            if sink in self._sinks:
                self._sinks.remove(sink)

    def enable_otel(self, service_name: str) -> None:
        from observability.tracing.otel import build_otel_tracer
        self._otel = build_otel_tracer(service_name)

    @contextmanager
    def span(self, kind: SpanKind, operation: str, *, input_reference: str | None = None,
             new_trace: bool = False, **attributes: Any) -> Iterator[SpanHandle]:
        trace_id = context.get("trace_id")
        if new_trace or trace_id is None:
            trace_id = uuid.uuid4().hex
        parent = None if new_trace else context.get("span_id")
        span = Span(
            trace_id=trace_id, span_id=uuid.uuid4().hex[:16], parent_span_id=parent,
            run_id=context.get("run_id"), agent_id=context.get("agent_id"), agent_tier=context.get("agent_tier"),
            kind=kind, operation=operation, start_time=datetime.now(timezone.utc), input_reference=input_reference,
        )
        handle = SpanHandle(span)
        handle.set(**attributes)
        started = time.perf_counter()
        otel_cm = self._otel.start_as_current_span(f"{kind.value}:{operation}") if self._otel else None
        otel_span = otel_cm.__enter__() if otel_cm else None
        try:
            with context.bind(trace_id=trace_id, span_id=span.span_id):
                yield handle
        except BaseException as exc:
            if span.status == "ok":
                handle.fail(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            span.end_time = datetime.now(timezone.utc)
            span.duration_ms = round((time.perf_counter() - started) * 1000, 3)
            if otel_span is not None:
                for key, value in {**span.model_dump(exclude={"attributes", "start_time", "end_time"}),
                                   **span.attributes}.items():
                    if value is not None:
                        otel_span.set_attribute(f"workbench.{key}", value if isinstance(value, (str, int, float, bool)) else str(value))
                otel_cm.__exit__(None, None, None)
            with self._lock:
                sinks = list(self._sinks)
            for sink in sinks:
                try:
                    sink.export(span)
                except Exception as exc:  # a telemetry sink failure must be visible but must not break the run
                    from observability.logging.logger import get_logger
                    get_logger().warning("trace sink %s failed: %s", type(sink).__name__, exc)


_tracer = Tracer()


def get_tracer() -> Tracer:
    return _tracer
