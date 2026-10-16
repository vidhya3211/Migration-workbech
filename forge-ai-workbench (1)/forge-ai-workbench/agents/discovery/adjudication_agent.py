"""Adjudication agent (T1): briefs the right named expert. It recommends nothing and decides nothing.

Each brief lays out the question, the three positions (source, target, document) side by side with their
exact evidence, the proven divergence and the exposure - then the actuary / product SME decides.

Where a model is permitted, it drafts a plain-English brief (summary, what each side computes, impact, the
questions to settle) from the same proven facts. The draft is advisory, may not recommend an option, and must
pass the grounding guardrail (no figure that is not in the facts) or it is discarded.
"""
from __future__ import annotations

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from pydantic import BaseModel, Field

from agents.base import AgentOutput, BaseAgent
from schemas.common import Tier, HumanRole, ReasoningMode, stable_id
from schemas.differential import AdjudicationBrief, DifferenceType, Differential
from schemas.signals import SignalType


class BriefDraft(BaseModel):
    summary: str
    source_reading: str = ""
    target_reading: str = ""
    impact: str = ""
    questions: list[str] = Field(default_factory=list)

OPTIONS = {
    DifferenceType.MISSING_IN_TARGET: ["accept_source", "reject", "override"],
    DifferenceType.MISSING_IN_SOURCE: ["accept_target", "reject", "override"],
}


@register_agent
class AdjudicationAgent(BaseAgent):
    IDENTITY = agent_identity("adjudication_agent", Tier.T1, "discovery", 'Briefs the right named expert - decides nothing itself', {S.ADJUDICATION_BRIEFS, S.CERTIFIED_RULES, S.HUMAN_REVIEW_REQUIRED}, llm={'adjudication_brief'})

    def run(self, differentials: list[Differential], **_: object) -> AgentOutput:
        briefs = []
        drafted = 0
        for d in differentials:
            divergence = "; ".join(f"{', '.join(f'{k}={v}' for k, v in v.inputs.items()) or 'all inputs'} -> "
                                   f"source {v.source_value} / target {v.target_value}" for v in d.divergence[:3])
            brief = AdjudicationBrief(
                brief_id=stable_id("BRF", self.run_id, d.differential_id), differential_id=d.differential_id,
                required_role=HumanRole.ACTUARY_SME,
                question=f"What is the correct rule for {d.canonical_name.replace('_', ' ').lower()}?",
                options=OPTIONS.get(d.difference_type, ["accept_source", "accept_target", "override"]),
                source_position=d.source_summary, target_position=d.target_summary,
                document_position=d.document_summary, evidence=d.evidence,
                exposure_note=(f"{d.difference_type.value.replace('_', ' ')}; typical difference {d.financial_exposure}; "
                               f"severity rank {d.severity.score:.2f}" + (f"; proven divergence: {divergence}" if divergence else "")))
            facts = "\n".join([d.explanation, f"source: {d.source_summary}", f"target: {d.target_summary}",
                                f"document: {d.document_summary}", f"divergence: {divergence}",
                                f"exposure: {brief.exposure_note}"])
            draft, meta = self.narrate("adjudication_brief", {
                "rule": d.canonical_name, "difference_type": d.difference_type.value,
                "source_system": d.source_system or "-", "target_system": d.target_system or "-",
                "source": d.source_summary or "-", "target": d.target_summary or "-",
                "document": d.document_summary or "-", "divergence": divergence or "-",
                "exposure": brief.exposure_note}, facts, BriefDraft,
                text_of=lambda p: " ".join([p.summary, p.source_reading, p.target_reading, p.impact, *p.questions]))
            if draft is not None:
                brief = brief.model_copy(update={"narrative": draft.model_dump(), "model_assisted": meta})
                drafted += 1
            briefs.append(brief)
        self.note_mode(ReasoningMode.DETERMINISTIC)
        self.signal(SignalType.ADJUDICATION_BRIEFS, f"{len(briefs)} brief(s) prepared for the actuary / product SME"
                    + (f"; {drafted} with a model-drafted summary" if drafted else ""),
                    item_refs=[b.differential_id for b in briefs], data={"model_drafted": drafted})
        return AgentOutput(items={"briefs": briefs})
