"""Orchestrator agent (T3): the identity under which the graph sequences work, runs the control-plane
self-test and owns the kill-switch. LangGraph does the orchestration; this agent records it."""
from __future__ import annotations

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from blackboard.control_plane import self_test as control_plane_self_test
from guardrails.deterministic.evidence import EvidenceVerifier
from guardrails.deterministic.guard import DeterministicGuard
from guardrails.deterministic.guard import self_test as guard_self_test
from schemas.common import Tier, ReasoningMode
from schemas.signals import SignalLevel, SignalType


class SelfTestFailed(RuntimeError):
    pass


@register_agent
class OrchestratorAgent(BaseAgent):
    IDENTITY = agent_identity("orchestrator_agent", Tier.T3, "cross_cutting", 'Sequences every agent; owns the kill-switch', {S.CERTIFIED_RULES, S.GUARDRAIL, S.HUMAN_REVIEW_REQUIRED})

    def run(self, action: str = "self_test", **_: object) -> AgentOutput:
        if action != "self_test":
            raise ValueError(action)
        results = control_plane_self_test() + guard_self_test(
            lambda reader, arts: DeterministicGuard(EvidenceVerifier(reader, arts)))
        failed = [label for label, ok in results if not ok]
        self.note_mode(ReasoningMode.DETERMINISTIC)
        if failed:
            self.signal(SignalType.ORCHESTRATION, "control-plane self-test FAILED: " + "; ".join(failed),
                        level=SignalLevel.ERROR, data={"failed": failed})
            raise SelfTestFailed("; ".join(failed))
        self.signal(SignalType.ORCHESTRATION, f"control-plane self-test passed ({len(results)} refusal checks)",
                    data={"checks": len(results)})
        return AgentOutput(summary={"checks": len(results)})
