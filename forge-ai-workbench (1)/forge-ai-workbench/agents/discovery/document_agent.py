"""Document agent (T2): records what product documents *claim* - never which claim is right.

Deterministic patterns first; sentences with figures no pattern understands go to the LLM when permitted
and available (evidence-cited, re-anchored to the real lines, then guarded) - otherwise to a human.
"""
from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from agents.discovery.common import make_rule
from context.headroom.policy import ContentClass
from reasoning.deterministic.naming import canonical_name
from reasoning.llm.gateway import Evidence as LLMEvidence
from reasoning.router.router import ReasoningTask
from schemas.artefact import Artefact
from schemas.common import Tier, ReasoningMode
from schemas.provenance import DocumentLocator
from schemas.rules import RuleKind
from schemas.signals import SignalLevel, SignalType
from tools.parsers.document import extract_assertions, lines_for

FORMATS = {".md": "md", ".txt": "txt", ".html": "html", ".htm": "html", ".docx": "docx", ".pdf": "pdf", ".rtf": "txt"}


class LLMAssertion(BaseModel):
    name: str
    value: str
    operator: str = "="
    condition: str | None = None
    line_start: int
    line_end: int
    anchor: str


class LLMAssertions(BaseModel):
    assertions: list[LLMAssertion] = Field(default_factory=list)


@register_agent
class DocumentAgent(BaseAgent):
    IDENTITY = agent_identity("document_agent", Tier.T2, "discovery", 'Records what product documents claim', {S.CANDIDATE_RULES}, tools={'evidence.read'}, llm={'extract_assertions'}, proposes={'rule'}, extractor=True, source_kind="document", dialect="document", handles={'document'})

    def run(self, artefact: Artefact, **_: object) -> AgentOutput:
        fmt = FORMATS.get(PurePosixPath(artefact.path).suffix.lower(), "txt")
        data = self.services.reader.bytes(artefact.blob_uri)
        text = None if fmt in ("pdf", "docx") else self.services.reader.text(artefact.blob_uri)
        doc_lines = lines_for(fmt, text, data)
        page_lines: dict[int | None, list[str]] = {}
        for dl in doc_lines:
            page_lines.setdefault(dl.page, []).append(dl.text)
        assertions, unparsed = extract_assertions(doc_lines, fmt)
        rules = []
        for a in assertions:
            lines = page_lines[a.span.page]
            excerpt = "\n".join(lines[a.span.line_start - 1:a.span.line_end])
            rules.append(make_rule(
                agent=self.name, tier=self.identity.tier, art=artefact, kind=RuleKind.ASSERTION, name=a.name,
                canonical=canonical_name(a.name), expression=a.expression,
                locator=DocumentLocator(file=artefact.path, page=a.span.page, section=a.span.section,
                                        line_start=a.span.line_start, line_end=a.span.line_end),
                excerpt=excerpt, anchor=a.anchor, confidence=a.confidence, reasons=[f"document pattern '{a.pattern}'"],
                conditions=[a.condition] if a.condition else [], notes=a.notes + [f"claim: \"{a.sentence}\""],
                context={"operator": a.operator, "value": a.value, "pattern": a.pattern, "sentence": a.sentence}))
        self.note_mode(ReasoningMode.DETERMINISTIC)
        if unparsed:
            rules.extend(self._llm_or_human(artefact, unparsed, page_lines))
        summary = {"format": fmt, "lines": len(doc_lines), "assertions": len(rules), "unparsed_sentences": len(unparsed),
                   "sections": sorted({dl.section for dl in doc_lines if dl.section})}
        self.signal(SignalType.CANDIDATE_RULES, f"{artefact.path}: {len(rules)} claim(s) recorded",
                    item_refs=[r.rule_id for r in rules], data=summary, artefact_id=artefact.artefact_id)
        return AgentOutput(items={"candidates": rules}, summary=summary)

    def _llm_or_human(self, art: Artefact, spans, page_lines) -> list:
        plan = self.plan(ReasoningTask(task="interpret document claims", deterministic_possible=False,
                                       needs_semantics=True))
        if plan.requires_human_interpretation:
            self.signal(SignalType.CANDIDATE_RULES, f"{art.path}: {len(spans)} sentence(s) with figures need a human "
                        "reading (no LLM permitted/available)", level=SignalLevel.WARN, artefact_id=art.artefact_id,
                        data={"spans": [{"page": s.page, "lines": [s.line_start, s.line_end]} for s in spans[:20]]})
            return []
        out = []
        for span in spans[:10]:
            lines = page_lines[span.page]
            ev = "\n".join(f"{n:>4} | {lines[n - 1]}" for n in range(span.line_start, span.line_end + 1))
            res = self.llm("extract_assertions", {"file": art.path, "section": span.section or "-"},
                           [LLMEvidence("document", ev, ContentClass.REGULATORY_EVIDENCE)], LLMAssertions)
            if res.parsed is None:
                continue
            for a in res.parsed.assertions:
                if not (span.line_start <= a.line_start <= a.line_end <= span.line_end):
                    continue
                excerpt = "\n".join(lines[a.line_start - 1:a.line_end])
                if a.value not in excerpt:
                    continue   # the figure must appear verbatim in the cited lines
                out.append(make_rule(
                    agent=self.name, tier=self.identity.tier, art=art, kind=RuleKind.ASSERTION, name=a.name,
                    canonical=canonical_name(a.name), expression=a.value,
                    locator=DocumentLocator(file=art.path, page=span.page, section=span.section,
                                            line_start=a.line_start, line_end=a.line_end),
                    excerpt=excerpt, anchor=a.anchor, confidence=0.5, reasons=["LLM reading of a document claim"],
                    conditions=[a.condition] if a.condition else [],
                    context={"operator": a.operator, "value": a.value, "pattern": "llm"}, mode=ReasoningMode.LLM,
                    model=res.record.model, prompt_version=res.record.prompt_version))
        return out
