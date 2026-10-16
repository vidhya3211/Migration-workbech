"""Retrieval agent (T3): keeps the search index over the certified core current. Mechanical."""
from __future__ import annotations

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from schemas.common import Tier, ReasoningMode
from schemas.signals import SignalType


@register_agent
class RetrievalAgent(BaseAgent):
    IDENTITY = agent_identity("retrieval_agent", Tier.T3, "cross_cutting", 'Keeps the search index over the certified core current', {S.RETRIEVAL_INDEX})

    def run(self, **_: object) -> AgentOutput:
        n = self.services.retrieval.refresh(self.run_id)
        self.note_mode(ReasoningMode.DETERMINISTIC)
        self.signal(SignalType.RETRIEVAL_INDEX, f"indexed {n} certified document(s)", data={"documents": n})
        return AgentOutput(items={}, summary={"documents": n})
