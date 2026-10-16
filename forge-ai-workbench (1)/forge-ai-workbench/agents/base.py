"""Agent base: the execution contract (§42) every agent runs under.

An agent never calls another agent. It reads typed inputs from the blackboard, uses only the tools its
identity allows, reasons with the minimum mechanism the router picks, and posts typed signals that the
control plane validates. Every execution produces an ``AgentExecution`` record, a trace, structured logs,
and - for failures - an explicit status. Failures are never swallowed and never become approval.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from blackboard.control_plane import validate_signal
from config.settings import use_settings
from blackboard.identity import AgentIdentity, get_identity
from guardrails.pipeline import GuardrailViolation
from observability import context
from observability.cost.ledger import BudgetExceeded, RunLedger
from observability.logging.logger import Event, log_event
from observability.tracing.tracer import SpanKind, get_tracer
from reasoning.ats.budget import ATSBudget
from reasoning.ats.engine import ATSEngine
from reasoning.ats.search_policy import SearchPolicy
from reasoning.ats.trace import ATSResult
from reasoning.heuristics.base import heuristic_calls
from reasoning.llm.gateway import Evidence as LLMEvidence
from reasoning.llm.gateway import LLMOutcome
from reasoning.router.router import ReasoningPlan, ReasoningTask, route
from schemas.common import ReasoningMode, new_id
from schemas.execution import AgentExecution, ExecutionStatus
from schemas.signals import Signal, SignalLevel, SignalType


class AgentFailed(RuntimeError):
    """Raised after an agent failure has been recorded. The graph turns it into an explicit failed state."""

    def __init__(self, agent: str, status: ExecutionStatus, message: str) -> None:
        super().__init__(f"{agent} {status.value}: {message}")
        self.agent, self.status = agent, status


@dataclass
class AgentOutput:
    items: dict[str, list] = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    execution: AgentExecution | None = None


class BaseAgent:
    name: str = ""

    def __init__(self, services: Any, run_id: str) -> None:
        self.services = services
        self.run_id = run_id
        self.identity: AgentIdentity = get_identity(self.name)
        self.ledger: RunLedger = services.ledger(run_id)
        self.execution = AgentExecution(execution_id=new_id("EXE"), run_id=run_id, agent_name=self.name,
                                        tier=self.identity.tier)
        self._signals: list[Signal] = []
        self._ats: list[ATSResult] = []

    # ------------------------------------------------------------------ lifecycle
    def execute(self, **inputs: Any) -> AgentOutput:
        started = time.perf_counter()
        h0 = heuristic_calls()
        self.execution.input_references = [str(v)[:120] for v in inputs.get("input_refs", [])]
        with use_settings(self.services.settings), \
                context.bind(agent_id=self.name, agent_tier=self.identity.tier.value, run_id=self.run_id):
            with get_tracer().span(SpanKind.AGENT, self.name, tier=self.identity.tier.value) as span:
                self.execution.trace_id = span.span.trace_id
                log_event(Event.AGENT_STARTED)
                try:
                    self.ledger.check_time()
                    out = self.run(**inputs)
                    self.execution.status = ExecutionStatus.COMPLETED
                except GuardrailViolation as exc:
                    self._fail(ExecutionStatus.BLOCKED, str(exc), span)
                    raise AgentFailed(self.name, ExecutionStatus.BLOCKED, str(exc)) from exc
                except BudgetExceeded as exc:
                    self._fail(ExecutionStatus.BUDGET_STOPPED, str(exc), span)
                    raise AgentFailed(self.name, ExecutionStatus.BUDGET_STOPPED, str(exc)) from exc
                except AgentFailed:
                    raise
                except Exception as exc:
                    self._fail(ExecutionStatus.FAILED, f"{type(exc).__name__}: {exc}", span)
                    raise AgentFailed(self.name, ExecutionStatus.FAILED, f"{type(exc).__name__}: {exc}") from exc
                finally:
                    self.execution.duration_ms = round((time.perf_counter() - started) * 1000, 3)
                    self.execution.heuristic_calls = heuristic_calls() - h0
                    self.services.kc.save_execution(self.execution)
                out.signals = self._signals
                out.execution = self.execution
                self.execution.output_signal = self._signals[-1].signal_id if self._signals else None
                self.services.kc.save_execution(self.execution)
                log_event(Event.AGENT_COMPLETED, signals=len(self._signals), duration_ms=self.execution.duration_ms)
                return out

    def _fail(self, status: ExecutionStatus, message: str, span) -> None:
        self.execution.status, self.execution.error = status, message[:2000]
        span.fail(message)
        log_event(Event.AGENT_FAILED, message[:500], status=status.value)
        try:
            self.signal(SignalType.ORCHESTRATION, f"{self.name} {status.value}: {message[:300]}",
                        level=SignalLevel.ERROR)
        except Exception:
            pass

    def run(self, **inputs: Any) -> AgentOutput:  # pragma: no cover - abstract
        raise NotImplementedError

    # ------------------------------------------------------------------ blackboard
    def signal(self, signal_type: SignalType, message: str, *, item_refs: list[str] | None = None,
               data: dict | None = None, level: SignalLevel = SignalLevel.INFO,
               artefact_id: str | None = None) -> Signal:
        sig = Signal(signal_id=new_id("SIG"), run_id=self.run_id, agent=self.name, tier=self.identity.tier,
                     signal_type=signal_type, level=level, message=message, item_refs=item_refs or [],
                     data=data or {}, artefact_id=artefact_id, trace_id=context.get("trace_id"))
        validate_signal(sig)
        self.services.kc.save_signal(sig)
        self._signals.append(sig)
        return sig

    # ------------------------------------------------------------------ reasoning helpers
    def plan(self, task: ReasoningTask) -> ReasoningPlan:
        task.llm_available = self.services.llm.available
        task.tier_allows_llm = bool(self.identity.llm_purposes)
        task.tier_allows_ats = self.identity.uses_ats
        p = route(task)
        for mode in p.route:
            if mode not in self.execution.reasoning_mode:
                self.execution.reasoning_mode.append(mode)
        return p

    def note_mode(self, mode: ReasoningMode) -> None:
        if mode not in self.execution.reasoning_mode:
            self.execution.reasoning_mode.append(mode)

    def ats(self, policy: SearchPolicy) -> ATSResult:
        if not self.identity.uses_ats:
            from blackboard.control_plane import ControlPlaneViolation
            raise ControlPlaneViolation(f"{self.name} is not permitted to use ATS")
        s = self.services.settings.ats
        budget = ATSBudget(**s.model_dump())
        res = ATSEngine(budget, self.services.guardrails, self.ledger, self.run_id, self.name).search(policy)
        self.execution.ats_calls.append(res.ats_run_id)
        self._ats.append(res)
        self.note_mode(ReasoningMode.ATS)
        return res

    def llm(self, purpose: str, variables: dict[str, str], evidence: list[LLMEvidence], output_model) -> LLMOutcome:
        variables = {"business_domain": self._business_domain(), **variables}
        outcome = self.services.llm.call(agent=self.name, run_id=self.run_id, purpose=purpose, variables=variables,
                                         evidence=evidence, output_model=output_model, ledger=self.ledger)
        self.execution.llm_calls.append(outcome.record)
        self.execution.headroom_metrics.extend(outcome.headroom)
        if outcome.record.status == "ok":
            self.note_mode(ReasoningMode.LLM)
        return outcome

    def _business_domain(self) -> str:
        pid = getattr(self.services, "project_id", None)
        proj = self.services.kc.get_project(pid) if pid else None
        return proj.business_domain if proj else "business"

    def narrate(self, purpose: str, variables: dict[str, str], facts: str, output_model, *,
                text_of=lambda parsed: "", allowed: frozenset[str] = frozenset(), value_of=lambda parsed: None,
                content_class=None):
        """Model-drafted prose where language genuinely helps a human (briefs, stories, impact notes).

        Returns ``(parsed, model_assisted)`` or ``(None, None)``: when no model is permitted/available, the call
        fails, or the draft fails the grounding guardrail (a figure not in the facts, a value outside the allowed
        set) - the agent then keeps its deterministic text. Never used for a decision."""
        from context.headroom.policy import ContentClass
        from guardrails.output.policies import GroundedNarrative
        from guardrails.pipeline import GuardrailContext, Stage
        if getattr(self, "_narration_off", False) or not (purpose in self.identity.llm_purposes
                                                           and self.services.llm.available):
            return None, None
        plan = self.plan(ReasoningTask(task=purpose, deterministic_possible=True, needs_semantics=True))
        if ReasoningMode.LLM not in plan.route:
            return None, None
        try:
            res = self.llm(purpose, variables,
                           [LLMEvidence("facts", facts, content_class or ContentClass.STRUCTURED_BULK)], output_model)
        except (BudgetExceeded, GuardrailViolation) as exc:
            # narration is optional: stop asking for the rest of this step and keep the deterministic text
            self._narration_off = True
            self.signal(SignalType.ORCHESTRATION, f"{self.name}: model drafting stopped ({type(exc).__name__}: "
                        f"{str(exc)[:200]}); deterministic text kept", level=SignalLevel.WARN)
            return None, None
        if res.parsed is None:
            return None, None
        try:
            self.services.guardrails.run(Stage.OUTPUT_SCHEMA, GroundedNarrative(text_of(res.parsed), facts, allowed,
                                                                                value_of(res.parsed)),
                                         GuardrailContext(self.run_id, self.name), only={"grounded_narrative"})
        except GuardrailViolation:
            return None, None
        return res.parsed, {"model": res.record.model, "prompt_version": res.record.prompt_version,
                            "guard": "grounded_narrative passed", "authority": "advisory - a named human decides"}

    def tool(self, name: str, arguments: dict) -> Any:
        return self.services.tools.call(self.name, self.run_id, name, arguments, ledger=self.ledger,
                                        records=self.execution.tools_used)

    def mcp(self, name: str, arguments: dict, approval_ref: str | None = None) -> Any:
        return self.services.mcp.invoke(self.name, self.run_id, name, arguments, ledger=self.ledger,
                                        records=self.execution.mcp_tools_used, approval_ref=approval_ref)
