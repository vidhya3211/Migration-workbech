"""Governed MCP invocation (§10, §31).

authorisation (control plane + tool policy) -> allow-list -> guardrails (tool call) -> human-approval gate
-> input validation -> rate limit -> circuit breaker -> run budget -> traced call with timeout/retry
-> output validation -> untrusted-output scanning (injection, PII) -> audit + telemetry -> MCPResult

MCP never bypasses guardrails, tier enforcement, provenance, audit or human approval. Its output is
untrusted input for whoever consumes it.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

from blackboard.control_plane import ControlPlaneViolation, validate_tool
from guardrails.injection.detector import scan
from guardrails.pii.detector import find_pii
from guardrails.pipeline import GuardrailContext, GuardrailPipeline, Stage
from guardrails.tool.policies import ToolCall
from observability.cost.ledger import RunLedger
from observability.logging.logger import Event, log_event
from observability.tracing.tracer import SpanKind, get_tracer
from schemas.common import new_id, value_ref
from schemas.execution import ToolCallRecord
from tools.mcp.client import MCPTimeout, MCPTransportError
from tools.mcp.models import MCPInvocation, MCPResult
from tools.mcp.registry import MCPRegistry

Recorder = Callable[[MCPInvocation, MCPResult, str], None]
_PY_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}


class MCPError(RuntimeError):
    pass


class MCPAuthorizationError(MCPError):
    pass


class MCPApprovalRequired(MCPError):
    pass


class MCPOutputRejected(MCPError):
    pass


class MCPRateLimited(MCPError):
    pass


class MCPCircuitOpen(MCPError):
    pass


def validate_input(schema: dict, args: dict) -> list[str]:
    problems = []
    props = schema.get("properties", {})
    for req in schema.get("required", []):
        if req not in args:
            problems.append(f"missing required argument {req!r}")
    for key, value in args.items():
        if props and key not in props:
            problems.append(f"unexpected argument {key!r}")
            continue
        expected = _PY_TYPES.get(props.get(key, {}).get("type", ""))
        if expected and not isinstance(value, expected):
            problems.append(f"argument {key!r} should be {props[key]['type']}")
    return problems


class MCPGateway:
    MAX_OUTPUT_CHARS = 200_000
    BREAKER_THRESHOLD = 3
    BREAKER_COOLDOWN_S = 30.0

    def __init__(self, registry: MCPRegistry, guardrails: GuardrailPipeline, recorder: Recorder | None = None,
                 injection_threshold: float = 0.6) -> None:
        self.registry = registry
        self.guardrails = guardrails
        self.recorder = recorder
        self.injection_threshold = injection_threshold
        self._calls: dict[str, deque] = {}
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def invoke(self, agent: str, run_id: str, tool: str, arguments: dict, *, ledger: RunLedger | None = None,
               records: list[ToolCallRecord] | None = None, approval_ref: str | None = None) -> MCPResult:
        definition = self.registry.get(tool)
        inv = MCPInvocation(invocation_id=new_id("MCPI"), correlation_id=new_id("COR"), run_id=run_id, agent=agent,
                            tool=tool, arguments_ref=value_ref(arguments), approval_ref=approval_ref)
        if definition is None:
            raise MCPAuthorizationError(f"MCP tool {tool!r} is not on the allow-list")
        try:
            validate_tool(agent, tool, mcp=True)
        except ControlPlaneViolation as exc:
            raise MCPAuthorizationError(str(exc)) from exc
        if agent not in definition.policy.allowed_agents:
            raise MCPAuthorizationError(f"{agent} is not in the allowed_agents policy of {tool}")
        ctx = GuardrailContext(run_id=run_id, agent=agent)
        self.guardrails.run(Stage.MCP, ToolCall(tool, arguments, mcp=True), ctx,
                            only={"tool_authorization", "tool_arguments"})
        if definition.policy.requires_human_approval and not approval_ref:
            raise MCPApprovalRequired(f"{tool} requires a recorded human approval reference")
        problems = validate_input(definition.input_schema, arguments)
        if problems:
            raise MCPError(f"invalid input for {tool}: {'; '.join(problems)}")
        self._rate_limit(tool, definition.policy.rate_limit_per_minute)
        self._breaker_check(definition.server)
        if ledger is not None:
            ledger.record_mcp_call()
        log_event(Event.MCP_STARTED, tool=tool, correlation_id=inv.correlation_id)
        started = time.perf_counter()
        client = self.registry.clients[definition.server]
        with get_tracer().span(SpanKind.MCP, tool, input_reference=inv.arguments_ref,
                               correlation_id=inv.correlation_id, server=definition.server) as span:
            result = MCPResult(invocation_id=inv.invocation_id, tool=tool, status="error")
            try:
                out = self._call_with_retry(client, definition.remote_name, arguments, definition.policy.timeout_seconds)
                self._breaker_success(definition.server)
            except MCPTimeout as exc:
                self._breaker_failure(definition.server)
                result.status, result.error = "timeout", str(exc)
                span.fail(str(exc))
                return self._finish(inv, result, started, records, "timeout")
            except MCPTransportError as exc:
                self._breaker_failure(definition.server)
                result.error = str(exc)
                span.fail(str(exc))
                return self._finish(inv, result, started, records, "failed")
            if out.is_error:
                result.error = out.text[:500] or "tool reported an error"
                span.fail(result.error)
                return self._finish(inv, result, started, records, "failed")
            text = out.text
            if len(text) > self.MAX_OUTPUT_CHARS:
                result.status, result.error = "rejected", "output exceeds size limit"
                return self._finish(inv, result, started, records, "rejected")
            injection = scan(text)
            if injection.score >= self.injection_threshold:
                result.status, result.error = "rejected", f"untrusted output failed injection scan {injection.hits}"
                self.guardrails.run(Stage.MCP, text, ctx, only={"prompt_injection"}, raise_on_block=False)
                span.fail(result.error)
                self._finish(inv, result, started, records, "rejected")
                raise MCPOutputRejected(result.error)
            if injection.hits:
                result.warnings.append(f"weak injection signals: {injection.hits}")
            pii = sorted({f.kind for f in find_pii(text)})
            if pii and definition.policy.data_classification in ("public", "internal"):
                result.warnings.append(f"PII in output of an {definition.policy.data_classification} tool: {pii}")
            result.status, result.text, result.structured = "ok", text, out.structured
            result.output_ref = value_ref(text)
            span.output(result.output_ref)
        return self._finish(inv, result, started, records, "ok")

    def _finish(self, inv: MCPInvocation, result: MCPResult, started: float, records: list[ToolCallRecord] | None,
                outcome: str) -> MCPResult:
        result.latency_ms = round((time.perf_counter() - started) * 1000, 3)
        if records is not None:
            records.append(ToolCallRecord(tool=inv.tool, kind="mcp", status=result.status, latency_ms=result.latency_ms,
                                          input_ref=inv.arguments_ref, output_ref=result.output_ref, error=result.error,
                                          correlation_id=inv.correlation_id))
        log_event(Event.MCP_COMPLETED if result.status == "ok" else Event.MCP_FAILED, result.error or "",
                  tool=inv.tool, status=result.status, correlation_id=inv.correlation_id)
        if self.recorder is not None:
            self.recorder(inv, result, outcome)
        return result

    def _call_with_retry(self, client, remote_name: str, arguments: dict, timeout: float):
        try:
            return client.call(remote_name, arguments, timeout)
        except MCPTimeout:
            raise
        except MCPTransportError:
            time.sleep(0.2)
            return client.call(remote_name, arguments, timeout)

    def _rate_limit(self, tool: str, per_minute: int) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._calls.setdefault(tool, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= per_minute:
                raise MCPRateLimited(f"{tool} rate limit {per_minute}/min reached")
            q.append(now)

    def _breaker_check(self, server: str) -> None:
        with self._lock:
            opened = self._opened_at.get(server)
            if opened is not None and time.monotonic() - opened < self.BREAKER_COOLDOWN_S:
                raise MCPCircuitOpen(f"circuit open for MCP server {server}")
            if opened is not None:
                self._opened_at.pop(server, None)   # half-open: allow one trial call

    def _breaker_failure(self, server: str) -> None:
        with self._lock:
            self._failures[server] = self._failures.get(server, 0) + 1
            if self._failures[server] >= self.BREAKER_THRESHOLD:
                self._opened_at[server] = time.monotonic()

    def _breaker_success(self, server: str) -> None:
        with self._lock:
            self._failures[server] = 0
