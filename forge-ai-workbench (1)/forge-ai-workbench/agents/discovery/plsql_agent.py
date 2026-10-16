"""PL/SQL agent (T1): parses package bodies / procedures / functions and proposes rules with evidence."""
from __future__ import annotations

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from agents.discovery.common import lines_excerpt, make_rule
from reasoning.deterministic.naming import canonical_name
from schemas.artefact import Artefact
from schemas.common import Tier, ReasoningMode
from schemas.provenance import CodeLocator
from schemas.signals import SignalLevel, SignalType
from tools.parsers.plsql import PlsqlParser, inputs_of, tokenize


@register_agent
class PlsqlAgent(BaseAgent):
    IDENTITY = agent_identity("plsql_agent", Tier.T1, "discovery", 'Parses PL/SQL, proposes rules with evidence', {S.CANDIDATE_RULES}, tools={'evidence.read'}, proposes={'rule'}, extractor=True, source_kind="code", dialect="plsql", handles={'plsql'})

    def run(self, artefact: Artefact, **_: object) -> AgentOutput:
        text = self.services.reader.text(artefact.blob_uri) or ""
        lines = text.split("\n")
        parser = PlsqlParser(tokenize(text)).run()
        rules = []
        for f in parser.findings:
            base = f.routine if f.is_return and f.routine else parser.aliases.get(f.target.upper(), f.target)
            symbol = ".".join(x for x in (f.unit, f.routine) if x) or None
            rules.append(make_rule(
                agent=self.name, tier=self.identity.tier, art=artefact, kind=f.kind, name=f.target,
                canonical=canonical_name(base, strip_verbs=f.is_return), expression=f.expression,
                locator=CodeLocator(file=artefact.path, line_start=f.start_line, line_end=f.end_line, symbol=symbol),
                excerpt=lines_excerpt(lines, f.start_line, f.end_line), anchor=f.anchor, confidence=f.confidence,
                reasons=f.reasons, conditions=f.conditions, inputs=inputs_of(f.expr_toks), notes=(
                    ["executes inside " + "; ".join(f.loops)] if f.loops else []),
                context={"unit": f.unit, "routine": f.routine, "returns": f.is_return, "aliases": parser.aliases}))
        if parser.skipped:
            self.signal(SignalType.CANDIDATE_RULES, f"{artefact.path}: {len(parser.skipped)} statement(s) outside a "
                        "routine were not modelled", level=SignalLevel.WARN, artefact_id=artefact.artefact_id,
                        data={"skipped": [{"line": ln, "why": w} for ln, w in parser.skipped[:20]]})
        self.note_mode(ReasoningMode.DETERMINISTIC)
        summary = {"units": parser.units, "routines": parser.routines, "tables": sorted(parser.tables),
                   "rules": len(rules)}
        self.signal(SignalType.CANDIDATE_RULES, f"{artefact.path}: {len(rules)} candidate rule(s)",
                    item_refs=[r.rule_id for r in rules], data=summary, artefact_id=artefact.artefact_id)
        return AgentOutput(items={"candidates": rules}, summary=summary)
