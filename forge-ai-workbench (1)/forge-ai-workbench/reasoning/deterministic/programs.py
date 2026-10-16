"""Builds executable ``Program``s from normalised rules (one per artefact) and dependency information."""
from __future__ import annotations

from collections import defaultdict

from reasoning.deterministic.equivalence import Program, Step
from reasoning.deterministic.expressions import ExpressionError, identifiers
from schemas.rules import NormalisedRule, RuleKind

EXECUTABLE = {RuleKind.CONSTANT, RuleKind.CALCULATION, RuleKind.ASSIGNMENT}


def _order(n: NormalisedRule) -> tuple:
    loc = n.evidence.locator
    return (getattr(loc, "line_start", 0), getattr(loc, "cell_range", ""))


def canonicalisable(n: NormalisedRule) -> bool:
    return not any("not canonicalisable" in x or "does not evaluate" in x for x in n.notes)


def build_programs(rules: list[NormalisedRule]) -> dict[str, Program]:
    """artefact_id -> Program. Workbook input cells only count as constants where code defines them too."""
    code_defined = {n.canonical_name for n in rules if n.source_kind == "code" and n.kind in EXECUTABLE}
    by_art: dict[str, list[NormalisedRule]] = defaultdict(list)
    for n in rules:
        if n.source_kind not in ("code", "spreadsheet") or n.kind not in EXECUTABLE or not canonicalisable(n):
            continue
        if n.source_kind == "spreadsheet" and n.kind == RuleKind.CONSTANT and n.canonical_name not in code_defined:
            continue   # a worked-example input cell, not a business constant
        if n.resolved_expression.replace(" ", "") == n.canonical_name:
            continue   # self-assignment (e.g. "return $x" of the variable itself)
        by_art[n.evidence.artefact_id].append(n)
    # lifted rate tables are visible to every program of the same system (a copybook's table is used by programs)
    import json
    tables: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for n in rules:
        if n.kind == RuleKind.TABLE and n.source_kind == "code":
            try:
                tables[(n.system.system_name, n.system.system_role.value)].setdefault(n.canonical_name,
                                                                                     json.loads(n.expression))
            except ValueError:
                continue
    programs: dict[str, Program] = {}
    for art_id, items in by_art.items():
        items.sort(key=_order)
        steps = [Step(rule_id=n.rule_id, target=n.canonical_name, expr=n.resolved_expression,
                      conditions=list(n.condition_signature), kind=n.kind.value, order=k)
                 for k, n in enumerate(items)]
        programs[art_id] = Program(key=art_id, label=items[0].evidence.locator.file,
                                   role=items[0].system.system_role.value, steps=steps,
                                   tables=dict(tables.get((items[0].system.system_name,
                                                           items[0].system.system_role.value), {})))
    return programs


def dependents(programs: list[Program], variable: str) -> set[str]:
    """Variables whose value (transitively) depends on ``variable``."""
    edges: dict[str, set[str]] = defaultdict(set)
    for p in programs:
        for s in p.steps:
            try:
                refs = identifiers(s.expr)
                for c in s.conditions:
                    refs |= identifiers(c)
            except ExpressionError:
                continue
            for r in refs:
                if r != s.target:
                    edges[r].add(s.target)
    out: set[str] = set()
    stack = [variable]
    while stack:
        v = stack.pop()
        for d in edges.get(v, ()):
            if d not in out:
                out.add(d)
                stack.append(d)
    return out
