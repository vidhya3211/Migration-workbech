"""Oracle DDL / schema agent (T2): structural rules from tables, constraints, defaults, views and triggers.

Distinct from PL/SQL procedural logic. Also produces the data dictionary (typed attributes with exact
schema locators) that the Mapping, Validation and Data-quality agents read.
"""
from __future__ import annotations

import re

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from agents.discovery.common import evidence_for, lines_excerpt, make_rule
from reasoning.deterministic.naming import canonical_name
from schemas.artefact import Artefact
from schemas.common import Tier, ReasoningMode, stable_id
from schemas.mapping import DictionaryEntry
from schemas.provenance import SchemaLocator
from schemas.rules import RuleKind
from schemas.signals import SignalType
from tools.parsers.ddl import allowed_values, parse


def _between(column: str, checks: list[str]) -> tuple[str, str] | None:
    """``CHECK (HOLDER_AGE BETWEEN 18 AND 75)`` -> ("18", "75") for HOLDER_AGE."""
    pattern = re.compile(rf"\b{re.escape(column)}\s+BETWEEN\s+(-?[\d.]+)\s+AND\s+(-?[\d.]+)", re.I)
    for ck in checks:
        m = pattern.search(ck)
        if m:
            return m.group(1), m.group(2)
    return None


@register_agent
class DdlAgent(BaseAgent):
    IDENTITY = agent_identity("ddl_agent", Tier.T2, "discovery", 'Recovers structural rules from Oracle schema objects', {S.CANDIDATE_RULES, S.DATA_DICTIONARY}, tools={'evidence.read'}, proposes={'rule'}, extractor=True, source_kind="schema", dialect="sql", handles={'oracle_ddl'})

    def run(self, artefact: Artefact, **_: object) -> AgentOutput:
        text = self.services.reader.text(artefact.blob_uri) or ""
        lines = text.split("\n")
        model = parse(text)
        rules, entries = [], []
        for t in model.tables:
            checks = [c for c in t.constraints if c.kind == "check"]
            pk_cols = {c.upper() for k in t.constraints if k.kind == "primary_key" for c in k.columns}
            for col in t.columns:
                rng = _between(col.name, [c.expression for c in checks] + col.checks)
                loc = SchemaLocator(file=artefact.path, schema_name=t.schema, object_name=t.name, column=col.name,
                                    line_start=col.line_start, line_end=col.line_end)
                excerpt = lines_excerpt(lines, col.line_start, col.line_end)
                domain = [v for ck in col.checks for v in allowed_values(ck, col.name)] or \
                         [v for ck in checks if col.name.upper() in (x.upper() for x in ck.columns)
                          for v in allowed_values(ck.expression, col.name)]
                entries.append(DictionaryEntry(
                    entry_id=stable_id("DD", self.run_id, artefact.system.system_role, t.name, col.name),
                    system=artefact.system, entity=t.name, field=col.name, data_type=col.data_type, length=col.length,
                    scale=col.scale, nullable=col.nullable, primary_key=col.name.upper() in pk_cols,
                    allowed_values=domain, range=rng, description=col.comment,
                    evidence=evidence_for(artefact, loc, excerpt)))
                if col.default is not None:
                    rules.append(make_rule(
                        agent=self.name, tier=self.identity.tier, art=artefact, kind=RuleKind.CONSTANT,
                        name=f"{t.name}.{col.name} DEFAULT", canonical=canonical_name(col.name) + "_DEFAULT",
                        expression=col.default, locator=loc, excerpt=excerpt, anchor=col.name, confidence=0.9,
                        reasons=["column DEFAULT clause"], context={"table": t.name, "column": col.name}))
                if not col.nullable:
                    rules.append(make_rule(
                        agent=self.name, tier=self.identity.tier, art=artefact, kind=RuleKind.CONSTRAINT,
                        name=f"{t.name}.{col.name} NOT NULL", canonical=canonical_name(col.name) + "_REQUIRED",
                        expression=f"{col.name} IS NOT NULL", locator=loc, excerpt=excerpt, anchor=col.name,
                        confidence=0.95, reasons=["NOT NULL / PRIMARY KEY column"],
                        context={"table": t.name, "column": col.name, "constraint": "not_null"}))
                for ck in col.checks:
                    rules.append(make_rule(
                        agent=self.name, tier=self.identity.tier, art=artefact, kind=RuleKind.CONSTRAINT,
                        name=f"{t.name}.{col.name} CHECK", canonical=canonical_name(col.name) + "_DOMAIN",
                        expression=ck, locator=loc, excerpt=excerpt, anchor=col.name, confidence=0.95,
                        reasons=["column CHECK constraint"], context={"table": t.name, "column": col.name,
                                                                      "constraint": "check", "values": domain}))
            for c in t.constraints:
                loc = SchemaLocator(file=artefact.path, schema_name=t.schema, object_name=t.name,
                                    column=c.columns[0] if len(c.columns) == 1 else None, constraint=c.name,
                                    line_start=c.line_start, line_end=c.line_end)
                col = c.columns[0] if c.columns else t.name
                rules.append(make_rule(
                    agent=self.name, tier=self.identity.tier, art=artefact, kind=RuleKind.CONSTRAINT,
                    name=f"{t.name}.{c.name or c.kind}", canonical=canonical_name(col) + f"_{c.kind.upper()}",
                    expression=c.expression, locator=loc, excerpt=lines_excerpt(lines, c.line_start, c.line_end),
                    anchor=c.columns[0] if c.columns else (c.name or t.name), confidence=0.95,
                    reasons=[f"table {c.kind.replace('_', ' ')} constraint"],
                    context={"table": t.name, "columns": c.columns, "constraint": c.kind}))
        for o in model.objects:
            loc = SchemaLocator(file=artefact.path, schema_name=o.schema, object_name=o.name, line_start=o.line_start,
                                line_end=o.line_end)
            rules.append(make_rule(
                agent=self.name, tier=self.identity.tier, art=artefact, kind=RuleKind.CONSTRAINT,
                name=f"{o.kind.upper()} {o.name}", canonical=canonical_name(o.name), expression=o.detail or o.kind,
                locator=loc, excerpt=lines_excerpt(lines, o.line_start, o.line_end), anchor=o.name, confidence=0.7,
                reasons=[f"{o.kind} definition (structural; behaviour reviewed with PL/SQL)"],
                context={"object": o.kind}))
        self.note_mode(ReasoningMode.DETERMINISTIC)
        summary = {"tables": [t.name for t in model.tables], "columns": len(entries),
                   "objects": [f"{o.kind}:{o.name}" for o in model.objects], "rules": len(rules)}
        self.signal(SignalType.DATA_DICTIONARY, f"{artefact.path}: {len(entries)} attribute(s) in "
                    f"{len(model.tables)} table(s)", item_refs=[e.entry_id for e in entries], data=summary,
                    artefact_id=artefact.artefact_id)
        self.signal(SignalType.CANDIDATE_RULES, f"{artefact.path}: {len(rules)} structural rule(s)",
                    item_refs=[r.rule_id for r in rules], artefact_id=artefact.artefact_id)
        return AgentOutput(items={"candidates": rules, "dictionary": entries}, summary=summary)
