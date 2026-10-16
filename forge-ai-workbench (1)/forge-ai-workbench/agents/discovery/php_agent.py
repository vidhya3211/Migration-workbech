"""PHP / Symfony agent (T2): reads the target system's existing logic.

Produces a full proposal of what the target currently computes; a specialist approves or rejects it.
Output shape is identical to every other extractor so the Normaliser/Differential compare rule-for-rule.
"""
from __future__ import annotations

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from tools.parsers.tables import php_tables
from agents.discovery.common import table_rule, lines_excerpt, make_rule
from reasoning.deterministic.naming import canonical_name
from schemas.artefact import Artefact
from schemas.common import Tier, ReasoningMode
from schemas.provenance import CodeLocator
from schemas.signals import SignalType
from tools.parsers.php import PhpParser, inputs_of, tokenize


@register_agent
class PhpAgent(BaseAgent):
    IDENTITY = agent_identity("php_agent", Tier.T2, "discovery", "Reads the target system's PHP/Symfony logic", {S.CANDIDATE_RULES}, tools={'evidence.read'}, proposes={'rule'}, extractor=True, source_kind="code", dialect="php", handles={'php'})

    def run(self, artefact: Artefact, **_: object) -> AgentOutput:
        text = self.services.reader.text(artefact.blob_uri) or ""
        lines = text.split("\n")
        parser = PhpParser(tokenize(text)).run()
        rules = []
        for f in parser.findings:
            conf, reasons = f.confidence, list(f.reasons)
            if f.kind == "constant" and f.target_key in parser.property_defaults:
                if f.target_key in parser.modified:
                    conf, reasons = 0.5, ["property default only; reassigned inside a method"]
                else:
                    conf = 0.85
                    reasons.append("never reassigned via $this->")
            inputs = inputs_of(f.rhs_toks + f.cond_toks)
            notes = [n for n in (self._resolve(i, f.cls, parser) for i in inputs) if n]
            if f.loops:
                notes.append("executes inside " + "; ".join(f.loops))
            base = f.function if f.is_return and f.function else f.name
            scope = f"{f.cls}::{f.function}" if f.cls and f.function else (f.function or f.cls)
            rules.append(make_rule(
                agent=self.name, tier=self.identity.tier, art=artefact, kind=f.kind, name=f.name,
                canonical=canonical_name(base, strip_verbs=f.is_return, owner=f.cls), expression=f.expression,
                locator=CodeLocator(file=artefact.path, line_start=f.start_line, line_end=f.end_line, symbol=scope),
                excerpt=lines_excerpt(lines, f.start_line, f.end_line), anchor=f.anchor, confidence=conf,
                reasons=reasons, conditions=f.conditions, inputs=inputs, notes=notes,
                context={"class": f.cls, "function": f.function, "condition_lines": f.condition_lines,
                         "returns": f.is_return}))
        tables = php_tables(text)
        rules += [table_rule(agent=self.name, tier=self.identity.tier, art=artefact, table=tb, lines=lines)
                  for tb in tables]
        self.note_mode(ReasoningMode.DETERMINISTIC)
        summary = {"classes": parser.classes, "tables": len(tables), "functions": parser.functions, "constants": len(parser.constants),
                   "property_defaults": len(parser.property_defaults),
                   "unconditional_data_moves": parser.data_moves, "rules": len(rules)}
        self.signal(SignalType.CANDIDATE_RULES, f"{artefact.path}: {len(parser.functions)} function(s), "
                    f"{len(rules)} candidate rule(s)", item_refs=[r.rule_id for r in rules], data=summary,
                    artefact_id=artefact.artefact_id)
        return AgentOutput(items={"candidates": rules}, summary=summary)

    @staticmethod
    def _resolve(inp: str, cls: str | None, parser: PhpParser) -> str | None:
        if inp.endswith("()"):
            return None
        if inp.startswith(("self::", "static::")) and cls:
            key = f"{cls}::{inp.split('::', 1)[1]}"
        elif inp.startswith("$this->") and cls:
            key = f"{cls}::${inp[len('$this->'):]}"
        else:
            key = inp
        hit = parser.constants.get(key)
        if hit is None and key not in parser.modified:
            hit = parser.property_defaults.get(key)
        return f"{inp} = {hit[0]} (declared line {hit[1]})" if hit else None
