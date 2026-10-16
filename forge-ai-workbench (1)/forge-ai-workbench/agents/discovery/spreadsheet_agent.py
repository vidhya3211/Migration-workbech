"""Spreadsheet agent (T2): recovers rules that exist only in a workbook - formulas and labelled inputs.

Every rule's locator is file / sheet / cell; the guard re-reads that exact cell before accepting it.
"""
from __future__ import annotations

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from agents.discovery.common import make_rule
from reasoning.deterministic.canonical import CanonicalError, excel
from reasoning.deterministic.naming import canonical_name
from schemas.artefact import Artefact
from schemas.common import Tier, ReasoningMode
from schemas.provenance import SheetLocator
from schemas.signals import SignalType
from tools.parsers.spreadsheet import extract


@register_agent
class SpreadsheetAgent(BaseAgent):
    IDENTITY = agent_identity("spreadsheet_agent", Tier.T2, "discovery", 'Recovers rules from workbook formulas', {S.CANDIDATE_RULES}, tools={'evidence.read'}, proposes={'rule'}, extractor=True, source_kind="spreadsheet", dialect="excel", handles={'spreadsheet'})

    def run(self, artefact: Artefact, **_: object) -> AgentOutput:
        findings, summary = extract(self.services.reader.bytes(artefact.blob_uri))
        labels = {(f.sheet, f.cell): f.label for f in findings}
        rules = []
        for f in findings:
            ctx: dict = {"sheet": f.sheet, "cell": f.cell, "raw": f.raw}
            if f.kind == "calculation":
                cell_labels = {cell: lab for (sheet, cell), lab in labels.items() if sheet == f.sheet}
                try:
                    ctx["canonical_expression"] = excel(f.raw, cell_labels)
                except CanonicalError as exc:
                    ctx["canonical_error"] = str(exc)
            rules.append(make_rule(
                agent=self.name, tier=self.identity.tier, art=artefact, kind=f.kind, name=f.label,
                canonical=canonical_name(f.label), expression=f.expression,
                locator=SheetLocator(file=artefact.path, sheet=f.sheet, cell_range=f.cell), excerpt=f.raw,
                anchor=f.label, confidence=f.confidence, reasons=f.reasons, inputs=f.inputs, context=ctx))
        self.note_mode(ReasoningMode.DETERMINISTIC)
        summary["rules"] = len(rules)
        self.signal(SignalType.CANDIDATE_RULES, f"{artefact.path}: {summary['formulas']} formula(s), "
                    f"{summary['constants']} labelled input(s)", item_refs=[r.rule_id for r in rules], data=summary,
                    artefact_id=artefact.artefact_id)
        return AgentOutput(items={"candidates": rules}, summary=summary)
