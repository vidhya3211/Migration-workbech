"""Assistant agent (T2): answers questions from certified content only, citing sources - or refuses (UC13).

Question -> input guardrails (injection, PII) -> certified retrieval -> compose (LLM if permitted and
available, otherwise extractive) -> citation-integrity + unsupported-claim guardrails -> answer or refusal.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from context.headroom.policy import ContentClass
from guardrails.output.policies import CitedAnswer
from guardrails.pii.detector import redact
from guardrails.pipeline import GuardrailContext, GuardrailViolation, Stage
from reasoning.llm.gateway import Evidence as LLMEvidence
from reasoning.router.router import ReasoningTask
from schemas.build import AssistantAnswer, Citation
from schemas.common import Tier, ReasoningMode
from schemas.signals import SignalType
from tools.search.index import terms

class Composed(BaseModel):
    answer: str
    citations: list[str] = Field(default_factory=list)
    refused: bool = False
    refusal_reason: str | None = None


@register_agent
class AssistantAgent(BaseAgent):
    IDENTITY = agent_identity("assistant_agent", Tier.T2, "cross_cutting", 'Answers from certified content only, citing sources', {S.ASSISTANT_ANSWER}, tools={'search.certified'}, llm={'compose_cited_answer'})

    def run(self, question: str, **_: object) -> AgentOutput:
        ctx = GuardrailContext(self.run_id, self.name)
        try:
            self.services.guardrails.run(Stage.INPUT, question, ctx, only={"prompt_injection"})
        except GuardrailViolation as exc:
            return self._done(AssistantAnswer(question="[withheld]", answer="", refused=True,
                                              refusal_reason=f"question blocked by guardrail: {exc}", mode="guardrail"))
        q, _ = redact(question)
        backend = self.services.retrieval.backend
        project_id = self.services.project_id or self.services.kc.run_project(self.run_id)
        hits = [h for h in self.services.retrieval.search(q, project_id=project_id, run_id=self.run_id, top=5)
                if h.score >= getattr(backend, "minimum_score", 0.0)]
        if getattr(backend, "requires_term_overlap", True):
            q_terms = set(terms(q))
            hits = [h for h in hits
                    if len(q_terms & set(terms(h.doc.title + " " + h.doc.text))) >= min(2, len(q_terms))]
        if not hits:
            return self._done(AssistantAnswer(question=q, answer="", refused=True, mode="extractive",
                                              refusal_reason="the certified knowledge core does not answer this question"))
        allowed = {h.doc.ref: h.doc.text for h in hits}
        plan = self.plan(ReasoningTask(task="compose cited answer", deterministic_possible=True, candidates=len(hits),
                                       needs_semantics=len(hits) > 1))
        answer = None
        if ReasoningMode.LLM in plan.route:
            res = self.llm("compose_cited_answer", {"question": q},
                           [LLMEvidence(h.doc.ref, f"[{h.doc.ref}] {h.doc.text}", ContentClass.SEARCH_RESULT) for h in hits],
                           Composed)
            if res.parsed is not None:
                answer = AssistantAnswer(question=q, answer=res.parsed.answer, refused=res.parsed.refused,
                                         refusal_reason=res.parsed.refusal_reason, mode="llm",
                                         citations=[Citation(ref=r, label=r, excerpt=allowed.get(r, "")[:300])
                                                    for r in res.parsed.citations])
        if answer is None:
            top = hits[0]
            confidence = top.score
            answer = AssistantAnswer(question=q, answer=f"{top.doc.title}: {top.doc.text}", refused=False,
                                     mode="extractive", confidence=min(1.0, confidence),
                                     citations=[Citation(ref=top.doc.ref, label=top.doc.title, excerpt=top.doc.text[:300])])
        try:
            self.services.guardrails.run(Stage.OUTPUT_SCHEMA, CitedAnswer(answer.answer, [c.ref for c in answer.citations],
                                                                          allowed, answer.refused), ctx,
                                         only={"citation_integrity", "unsupported_claims"})
        except GuardrailViolation as exc:
            answer = AssistantAnswer(question=q, answer="", refused=True, mode=answer.mode,
                                     refusal_reason=f"draft answer failed grounding checks: {exc}")
        return self._done(answer)

    def _done(self, answer: AssistantAnswer) -> AgentOutput:
        self.signal(SignalType.ASSISTANT_ANSWER, "refused" if answer.refused else f"answered with "
                    f"{len(answer.citations)} citation(s)", item_refs=[c.ref for c in answer.citations],
                    data={"refused": answer.refused, "mode": answer.mode})
        return AgentOutput(items={"answer": [answer]})
