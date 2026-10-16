"""Helpers shared by the extractor agents: one ``CandidateRule`` shape for every parser (§33)."""
from __future__ import annotations

from schemas.artefact import Artefact
from schemas.common import ReasoningMode, Tier, stable_id
from schemas.provenance import CodeLocator, DocumentLocator, Evidence, SchemaLocator, SheetLocator
from schemas.rules import CandidateRule, RuleKind


def evidence_for(art: Artefact, locator, excerpt: str) -> Evidence:
    return Evidence(artefact_id=art.artefact_id, locator=locator, excerpt=excerpt, source_sha256=art.sha256,
                    system=art.system)


def lines_excerpt(lines: list[str], start: int, end: int) -> str:
    return "\n".join(lines[start - 1:end])


def make_rule(*, agent: str, tier: Tier, art: Artefact, kind: RuleKind | str, name: str, canonical: str,
              expression: str, locator: CodeLocator | SheetLocator | DocumentLocator | SchemaLocator, excerpt: str,
              anchor: str, confidence: float, reasons: list[str], conditions: list[str] | None = None,
              inputs: list[str] | None = None, notes: list[str] | None = None, context: dict | None = None,
              mode: ReasoningMode = ReasoningMode.DETERMINISTIC, model: str | None = None,
              prompt_version: str | None = None) -> CandidateRule:
    line = getattr(locator, "line_start", None) or getattr(locator, "cell_range", "")
    return CandidateRule(
        rule_id=stable_id("R", art.run_id, art.sha256, kind, name.upper(), line, expression),
        run_id=art.run_id, artefact_id=art.artefact_id, agent=agent, tier=tier, kind=RuleKind(kind), name=name,
        canonical_name=canonical, expression=expression, conditions=conditions or [], inputs=inputs or [],
        evidence=evidence_for(art, locator, excerpt), anchor=anchor,
        confidence=round(max(0.0, min(1.0, confidence)), 2), confidence_reasons=reasons, notes=notes or [],
        context=context or {}, system=art.system, language=art.language, requires_human=True,
        reasoning_mode=mode, model=model, prompt_version=prompt_version,
    )


def table_rule(*, agent: str, tier, art: Artefact, table, lines: list[str]) -> CandidateRule:
    """A lifted rate/lookup table as one rule: entries in ``context["entries"]`` and a stable compact expression."""
    import json as _json

    from reasoning.deterministic.naming import canonical_name
    excerpt = lines_excerpt(lines, table.start_line, table.end_line)
    anchor = table.name if table.name.upper() in excerpt.upper() else excerpt.strip().split()[0] if excerpt.strip() else table.name
    return make_rule(
        agent=agent, tier=tier, art=art, kind="table", name=table.name, canonical=canonical_name(table.name),
        expression=_json.dumps(table.entries, sort_keys=True, separators=(",", ":")),
        locator=CodeLocator(file=art.path, line_start=table.start_line, line_end=table.end_line, symbol=table.name),
        excerpt=excerpt, anchor=anchor, confidence=0.9,
        reasons=[f"lookup table of {len(table.entries)} entr(ies), indexed by {table.index_by}"] + table.notes,
        context={"entries": table.entries, "index_by": table.index_by, "value_field": table.value_field})
