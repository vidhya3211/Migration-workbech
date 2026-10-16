"""Native (internal, deterministic) tools and the tool router (§10).

    Agent -> ToolRouter -> native tool (internal capability)
                        -> MCP gateway (enterprise capability)

Every call is authorised against the agent identity, guarded, traced, logged and counted in the run
ledger. Tool results are untrusted until validated against the tool's output model.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel

from guardrails.pipeline import GuardrailContext, GuardrailPipeline, Stage
from guardrails.tool.policies import ToolCall
from observability.cost.ledger import RunLedger
from observability.logging.logger import Event, log_event
from observability.tracing.tracer import SpanKind, get_tracer
from schemas.common import value_ref
from schemas.execution import ToolCallRecord


class ToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    fn: Callable[[BaseModel], BaseModel]


class NativeToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)


class ToolRouter:
    def __init__(self, native: NativeToolRegistry, guardrails: GuardrailPipeline, mcp_gateway: Any = None) -> None:
        self.native = native
        self.guardrails = guardrails
        self.mcp = mcp_gateway

    def call(self, agent: str, run_id: str, tool: str, arguments: dict, *, ledger: RunLedger | None = None,
             records: list[ToolCallRecord] | None = None) -> Any:
        if self.native.get(tool) is None:
            if self.mcp is None:
                raise ToolError(f"unknown tool {tool!r}")
            return self.mcp.invoke(agent, run_id, tool, arguments, ledger=ledger, records=records)
        spec = self.native.get(tool)
        ctx = GuardrailContext(run_id=run_id, agent=agent)
        self.guardrails.run(Stage.TOOL, ToolCall(tool, arguments), ctx)
        if ledger is not None:
            ledger.record_tool_call()
        started = time.perf_counter()
        in_ref = value_ref(arguments)
        log_event(Event.TOOL_STARTED, tool=tool, version=spec.version)
        with get_tracer().span(SpanKind.TOOL, tool, input_reference=in_ref, version=spec.version) as span:
            try:
                result = spec.output_model.model_validate(spec.fn(spec.input_model.model_validate(arguments)))
            except Exception as exc:
                rec = ToolCallRecord(tool=tool, kind="native", status="failed",
                                     latency_ms=(time.perf_counter() - started) * 1000, input_ref=in_ref,
                                     error=f"{type(exc).__name__}: {exc}")
                if records is not None:
                    records.append(rec)
                log_event(Event.TOOL_FAILED, str(exc)[:300], tool=tool)
                raise ToolError(f"{tool} failed: {exc}") from exc
            out_ref = value_ref(result.model_dump(mode="json"))
            span.output(out_ref)
        rec = ToolCallRecord(tool=tool, kind="native", status="ok", latency_ms=(time.perf_counter() - started) * 1000,
                             input_ref=in_ref, output_ref=out_ref)
        if records is not None:
            records.append(rec)
        log_event(Event.TOOL_COMPLETED, tool=tool, latency_ms=round(rec.latency_ms, 2))
        return result
