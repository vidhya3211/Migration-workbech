"""Differential agent (T2): flags every source/target disagreement, ranked by financial exposure (UC5).

    deterministic  - build programs per artefact; *prove* divergence with test vectors (equivalence engine)
    heuristic      - rank by financial exposure (severity heuristic, never a decision)
    ATS            - explore alternative interpretations of *why* a variable diverges; keep the best-evidenced
    LLM (optional) - a plain-English explanation of the proven facts for the actuary
    guard          - every differential's evidence re-verifies against the raw source before it is stored

It proposes; a named actuary / product SME decides (Stage 3).
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from pydantic import BaseModel

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from context.headroom.policy import ContentClass
from reasoning.ats.policies import DifferentialCausePolicy
from reasoning.deterministic.equivalence import Comparison, Program, compare, simulate
from reasoning.deterministic.naming import canonical_name
from reasoning.deterministic.expressions import ExpressionError, identifiers
from reasoning.deterministic.programs import build_programs, dependents
from reasoning.heuristics.differential.severity import score as severity
from reasoning.llm.gateway import Evidence as LLMEvidence
from reasoning.router.router import ReasoningTask
from schemas.common import Tier, ReasoningMode, SystemRole, stable_id
from schemas.differential import DifferenceType, Differential, DivergenceVector
from schemas.rules import NormalisedRule, RuleKind
from schemas.signals import SignalType

CATEGORY_TYPE = {"value": DifferenceType.VALUE_MISMATCH, "table": DifferenceType.VALUE_MISMATCH,
                 "condition": DifferenceType.CONDITION_MISMATCH,
                 "expression": DifferenceType.EXPRESSION_MISMATCH, "rounding": DifferenceType.EXPRESSION_MISMATCH,
                 "unproven": DifferenceType.EXPRESSION_MISMATCH}


class Explanation(BaseModel):
    explanation: str


def _fmt(d: Decimal) -> str:
    """A money-like figure for people: no float noise, at most 6 decimal places, trailing zeros dropped."""
    return format(d.quantize(Decimal("0.000001")).normalize(), "f")


def _dec(v: str | None) -> Decimal | None:
    try:
        return Decimal(v) if v is not None else None
    except Exception:
        return None


@register_agent
class DifferentialAgent(BaseAgent):
    IDENTITY = agent_identity("differential_agent", Tier.T2, "discovery", 'Flags disagreements ranked by financial exposure', {S.DIFFERENTIALS}, tools={'deterministic.expression'}, llm={'explain_difference'}, ats=True, proposes={'differential'})

    def run(self, normalised: list[NormalisedRule], **_: object) -> AgentOutput:
        by_id = {n.rule_id: n for n in normalised}
        programs = build_programs(normalised)
        src = [p for p in programs.values() if p.role == SystemRole.SOURCE.value]
        tgt = [p for p in programs.values() if p.role == SystemRole.TARGET.value]
        self.plan(ReasoningTask(task="source/target comparison", deterministic_possible=True,
                                candidates=len(src) * len(tgt)))
        docs: dict[str, list[NormalisedRule]] = defaultdict(list)
        for n in normalised:
            if n.source_kind == "document":
                docs[n.canonical_name].append(n)
        src_vars = {v for p in src for v in p.assigned()}
        tgt_vars = {v for p in tgt for v in p.assigned()}
        diffs: list[Differential] = []
        folded: dict[str, list[str]] = defaultdict(list)

        shared = sorted(src_vars & tgt_vars)
        for var in shared:
            comps = [compare(var, s, t) for s in src if var in s.assigned() for t in tgt if var in t.assigned()]
            code = [c for c in comps if not programs[c.source_key].label.lower().endswith((".xlsx", ".xlsm"))]
            workbook = [c for c in comps if c not in code]
            diverging = [c for c in code if c.diverges or c.category == "unproven"]
            if diverging:
                diffs.append(self._differential(var, diverging + [c for c in workbook if c.diverges], programs,
                                                by_id, docs, src + tgt))
            elif code and any(c.diverges for c in workbook):
                # legacy code agrees with the target; only the workbook disagrees -> a source-document conflict
                diffs.append(self._differential(var, [c for c in workbook if c.diverges], programs, by_id, docs,
                                                src + tgt, workbook_only=True))
            elif not code and any(c.diverges or c.category == "unproven" for c in workbook):
                diffs.append(self._differential(var, [c for c in workbook if c.diverges or c.category == "unproven"],
                                                programs, by_id, docs, src + tgt))

        for direction, own, missing in ((DifferenceType.MISSING_IN_TARGET, src, sorted(src_vars - tgt_vars)),
                                        (DifferenceType.MISSING_IN_SOURCE, tgt, sorted(tgt_vars - src_vars))):
            for var in self._roots(missing, own):
                users = {v for v in shared if var in self._deps_of(v, own)}
                referencing = [d for d in diffs if d.canonical_name in users]
                if referencing:
                    folded[referencing[0].differential_id].append(var)
                elif users:
                    continue   # a helper used only by variables proven equivalent: implementation detail
                else:
                    diffs.append(self._missing(var, direction, own, programs, by_id, docs, missing))
        for d in diffs:
            if d.differential_id in folded:
                d.explanation += f" Related source-only item(s) with no target equivalent: {', '.join(folded[d.differential_id])}."
                for var in folded[d.differential_id]:
                    d.source_rule_ids += [s.rule_id for p in src for s in p.steps_for(var)]
                    d.evidence += [by_id[s.rule_id].evidence for p in src for s in p.steps_for(var)]
        diffs += self._document_conflicts(docs, src, diffs, by_id)
        diffs += self._table_differences(normalised)
        diffs = [d for d in diffs if d.evidence]
        diffs.sort(key=lambda d: -d.severity.score)
        self._explain_with_llm(diffs)
        self.signal(SignalType.DIFFERENTIALS, f"{len(diffs)} disagreement(s) between source and target",
                    item_refs=[d.differential_id for d in diffs],
                    data={"by_type": _count(d.difference_type.value for d in diffs),
                          "top": [f"{d.canonical_name} ({d.difference_type.value}, {d.severity.score:.2f})" for d in diffs[:5]]})
        return AgentOutput(items={"differentials": diffs})

    # ------------------------------------------------------------------ builders
    def _differential(self, var: str, comps: list[Comparison], programs: dict[str, Program],
                      by_id: dict[str, NormalisedRule], docs, all_programs: list[Program],
                      workbook_only: bool = False) -> Differential:
        c0 = comps[0]
        s_prog, t_prog = programs[c0.source_key], programs[c0.target_key]
        s_rules = [by_id[s.rule_id] for c in comps for s in programs[c.source_key].steps_for(var)]
        t_rules = [by_id[s.rule_id] for c in comps for s in programs[c.target_key].steps_for(var)]
        s_rules = list({r.rule_id: r for r in s_rules}.values())
        t_rules = list({r.rule_id: r for r in t_rules}.values())
        dtype = DifferenceType.DOCUMENT_CONFLICT if workbook_only else CATEGORY_TYPE[c0.category]
        ats = self.ats(DifferentialCausePolicy(
            var, {"divergent": c0.divergent, "category": c0.category, "max_abs_delta": str(c0.max_abs_delta)},
            [{"expr": s.expr, "conditions": s.conditions} for s in s_prog.steps_for(var)],
            [{"expr": s.expr, "conditions": s.conditions} for s in t_prog.steps_for(var)]))
        cause = ats.best.hypothesis if ats.best else "cause not isolated"
        relevant = self._relevant_inputs(var, [s_prog, t_prog])
        vectors = [DivergenceVector(inputs={k: v for k, v in d["inputs"].items() if v is not None and k in relevant},
                                    source_value=d["source"], target_value=d["target"]) for d in c0.divergent[:5]]
        ex = self._example(c0.divergent, relevant)
        s_desc = "; ".join(f"{r.resolved_expression}" + (f" WHEN {' AND '.join(r.condition_signature)}"
                                                         if r.condition_signature else "") for r in s_rules)
        t_desc = "; ".join(f"{r.resolved_expression}" + (f" WHEN {' AND '.join(r.condition_signature)}"
                                                         if r.condition_signature else "") for r in t_rules)
        if c0.category == "unproven":
            text = f"{var}: equivalence of source and target logic could not be proved deterministically."
        else:
            example = ", ".join(f"{k}={v}" for k, v in (ex["inputs"] if ex else {}).items() if v is not None)
            when = f" with {example}" if example else ""
            text = (f"{var} ({s_prog.label} vs {t_prog.label}): {cause}. Proven on {c0.divergent_count} of "
                    f"{c0.vectors_known} test vectors" + (f"; e.g.{when}: source {ex['source']} vs target "
                                                          f"{ex['target']}." if ex else "."))
            if c0.category == "rounding":
                text += " Differences are within one penny (rounding / precision)."
            if len(comps) > 1:
                text += f" {len(comps)} source/target program pairs disagree."
            if workbook_only:
                text += (" The legacy source code is proven equivalent to the target here; only the source workbook "
                         "disagrees (a worked example or an outdated spreadsheet rule).")
        if c0.assumptions:
            text += " Assumption: " + "; ".join(c0.assumptions) + "."
        doc_rules = self._related_docs(var, s_rules + t_rules, docs)
        deltas = sorted({str(abs(_dec(d["target"]) - _dec(d["source"])))
                         for c in comps for d in c.divergent if _dec(d["target"]) is not None and _dec(d["source"]) is not None})
        reference = _dec(ex["source"]) if ex else None
        share = sum(c.divergent_count for c in comps) / max(1, sum(c.vectors_known for c in comps))
        typical = c0.median_abs_delta
        book = None if workbook_only else self._real_book(var, s_prog, t_prog)
        sev = severity(var, s_rules[0].domain if s_rules else "general",
                       "rounding" if c0.category == "rounding" else dtype.value,
                       None if workbook_only else typical, reference, share,
                       book["affected"] if book else None, cfg=self._severity_cfg())
        if book:
            text += (f" On the real book: {book['affected']} of {book['records_evaluated']} source record(s) in "
                     f"{book['extract']} get a different {var}"
                     + (f" (median difference {book['delta_median']}, largest {book['delta_max']}, total "
                        f"{book['delta_total']})" if book["affected"] and book.get("delta_median") is not None else "")
                     + (" - sampled" if book["sampled"] else "") + ".")
        impacts = sorted(dependents(all_programs, var))
        evidence = [r.evidence for r in s_rules + t_rules + doc_rules]
        return Differential(
            differential_id=stable_id("DIF", self.run_id, var, dtype.value), run_id=self.run_id, canonical_name=var,
            domain=s_rules[0].domain if s_rules else "general", difference_type=dtype,
            source_rule_ids=[r.rule_id for r in s_rules], target_rule_ids=[r.rule_id for r in t_rules],
            document_rule_ids=[r.rule_id for r in doc_rules], source_summary=s_desc, target_summary=t_desc,
            document_summary="; ".join(f"{r.expression} ({r.condition_signature or 'always'})" for r in doc_rules) or None,
            source_system=s_rules[0].system.system_name if s_rules else None,
            target_system=t_rules[0].system.system_name if t_rules else None, explanation=text, divergence=vectors,
            severity=sev.model_copy(update={"evidence": sev.evidence + [f"ATS: {cause}", f"impacts: {impacts[:8]}",
                                                                        f"deltas: {deltas[:5]}"]}),
            financial_exposure=("n/a (workbook)" if workbook_only else
                                format(typical.normalize(), "f") if typical is not None else "unquantified"),
            real_book=book, evidence=evidence)

    def _missing(self, var: str, dtype: DifferenceType, progs: list[Program], programs, by_id, docs,
                 group: list[str]) -> Differential:
        rules = [by_id[s.rule_id] for p in progs for s in p.steps_for(var)]
        deps = sorted(v for v in group if v != var and var in dependents(progs, v))   # what var is built from
        for d in deps:
            rules += [by_id[s.rule_id] for p in progs for s in p.steps_for(d)]
        rules = list({r.rule_id: r for r in rules}.values())
        side = "source" if dtype is DifferenceType.MISSING_IN_TARGET else "target"
        other = "target" if side == "source" else "source"
        doc_rules = self._related_docs(var, rules, docs)
        text = (f"{var} is computed by the {side} ({'; '.join(r.resolved_expression for r in rules[:3])}) but no {other} "
                f"logic computes it" + (f"; it depends on {', '.join(deps)}" if deps else "") + ".")
        sev = severity(var, rules[0].domain if rules else "general", dtype.value, None, None, 1.0, None,
                       cfg=self._severity_cfg())
        return Differential(
            differential_id=stable_id("DIF", self.run_id, var, dtype.value), run_id=self.run_id, canonical_name=var,
            domain=rules[0].domain if rules else "general", difference_type=dtype,
            source_rule_ids=[r.rule_id for r in rules] if side == "source" else [],
            target_rule_ids=[r.rule_id for r in rules] if side == "target" else [],
            document_rule_ids=[r.rule_id for r in doc_rules],
            source_summary="; ".join(r.resolved_expression for r in rules) if side == "source" else None,
            target_summary="; ".join(r.resolved_expression for r in rules) if side == "target" else None,
            document_summary="; ".join(r.expression for r in doc_rules) or None,
            source_system=rules[0].system.system_name if rules and side == "source" else None,
            target_system=rules[0].system.system_name if rules and side == "target" else None,
            explanation=text, severity=sev, financial_exposure="unquantified",
            evidence=[r.evidence for r in rules + doc_rules])

    def _document_conflicts(self, docs, src: list[Program], diffs: list[Differential],
                            by_id: dict[str, NormalisedRule]) -> list[Differential]:
        covered = {d.canonical_name for d in diffs}
        out = []
        for var, drules in docs.items():
            if var in covered:
                continue
            code_values = {s.expr for p in src for s in p.steps_for(var)}
            code_nums = {_dec(v) for v in code_values if _dec(v) is not None}
            for dr in drules:
                dv = _dec(dr.constant_value or dr.resolved_expression)
                if dv is None or not code_nums or dv in code_nums:
                    continue
                code_rules = [by_id[s.rule_id] for p in src for s in p.steps_for(var)]
                sev = severity(var, dr.domain, DifferenceType.DOCUMENT_CONFLICT.value,
                               min(abs(dv - c) for c in code_nums), dv, 1.0, None, cfg=self._severity_cfg())
                out.append(Differential(
                    differential_id=stable_id("DIF", self.run_id, var, "document", dr.rule_id), run_id=self.run_id,
                    canonical_name=var, domain=dr.domain, difference_type=DifferenceType.DOCUMENT_CONFLICT,
                    source_rule_ids=[r.rule_id for r in code_rules], document_rule_ids=[dr.rule_id],
                    source_summary="; ".join(sorted(code_values)), document_summary=dr.expression,
                    explanation=f"{var}: the product document states {dr.expression} but the source code uses "
                                f"{', '.join(sorted(code_values))}.",
                    severity=sev, financial_exposure=str(min(abs(dv - c) for c in code_nums)),
                    evidence=[dr.evidence] + [r.evidence for r in code_rules]))
        return out

    # ------------------------------------------------------------------ helpers
    def _table_differences(self, normalised: list[NormalisedRule]) -> list[Differential]:
        """Rate/lookup tables lifted from source and target code with the same canonical name, compared entry by
        entry: entries that differ, and entries only one side has (a rate the target cannot look up)."""
        import json
        tables: dict[str, dict[str, NormalisedRule]] = defaultdict(dict)
        for n in normalised:
            if n.kind == RuleKind.TABLE and n.source_kind == "code":
                tables[n.canonical_name].setdefault(n.system.system_role.value, n)
        out = []
        for name, sides in sorted(tables.items()):
            s, t = sides.get(SystemRole.SOURCE.value), sides.get(SystemRole.TARGET.value)
            if s is None or t is None:
                continue
            try:
                se, te = json.loads(s.expression), json.loads(t.expression)
            except ValueError:
                continue
            def same(x: str, y: str) -> bool:      # numbers compare by value (1.5 == 1.50), text exactly
                dx, dy = _dec(x), _dec(y)
                return dx == dy if dx is not None and dy is not None else x.strip() == y.strip()
            differ = sorted(k for k in set(se) & set(te) if not same(se[k], te[k]))
            only_s, only_t = sorted(set(se) - set(te)), sorted(set(te) - set(se))
            if not (differ or only_s or only_t):
                continue
            deltas = [abs(_dec(te[k]) - _dec(se[k])) for k in differ if _dec(se[k]) is not None and _dec(te[k]) is not None]
            text = (f"Lookup table {name}: {len(se)} source entr(ies), {len(te)} target entr(ies). "
                    + (f"{len(differ)} entr(ies) hold different values ({', '.join(differ[:6])}). " if differ else "")
                    + (f"Only in source: {', '.join(only_s[:6])} - the target cannot look these up. " if only_s else "")
                    + (f"Only in target: {', '.join(only_t[:6])}." if only_t else "")).strip()
            vectors = [DivergenceVector(inputs={"key": k}, source_value=se.get(k), target_value=te.get(k))
                       for k in (differ + only_s + only_t)[:5]]
            dtype = DifferenceType.VALUE_MISMATCH if differ else DifferenceType.MISSING_IN_TARGET if only_s \
                else DifferenceType.MISSING_IN_SOURCE
            sev = severity(name, s.domain, dtype.value, max(deltas) if deltas else None,
                           _dec(se[differ[0]]) if differ else None,
                           (len(differ) + len(only_s) + len(only_t)) / max(1, len(set(se) | set(te))), None,
                           cfg=self._severity_cfg())
            out.append(Differential(
                differential_id=stable_id("DIF", self.run_id, name, "table"), run_id=self.run_id, canonical_name=name,
                domain=s.domain, difference_type=dtype, source_rule_ids=[s.rule_id], target_rule_ids=[t.rule_id],
                source_summary=f"table of {len(se)}: " + ", ".join(f"{k}={v}" for k, v in list(se.items())[:8]),
                target_summary=f"table of {len(te)}: " + ", ".join(f"{k}={v}" for k, v in list(te.items())[:8]),
                source_system=s.system.system_name, target_system=t.system.system_name, explanation=text,
                divergence=vectors, severity=sev,
                financial_exposure=format(sorted(deltas)[len(deltas) // 2], "f") if deltas else "unquantified",
                evidence=[s.evidence, t.evidence]))
        return out

    def _severity_cfg(self) -> dict:
        return (self.services.settings.heuristics.get("differential") or {}).get("severity") or {}

    def _real_book(self, var: str, s_prog: Program, t_prog: Program) -> dict | None:
        """Run both rules over the real staged source records whose columns supply every input of ``var``: how many
        records get a different result, and by how much.

        Records are grouped by their input values in PostgreSQL first - a book of a million policies usually has a
        few thousand distinct input combinations - so each combination is executed once and weighted by its
        record count: exact, and independent of the book's size. ``heuristics.differential.real_book_rows`` caps
        the number of distinct combinations (the result says when it is a sample)."""
        cap = int((self.services.settings.heuristics.get("differential") or {}).get("real_book_rows", 200_000))
        assigned = s_prog.assigned() | t_prog.assigned()
        inputs = sorted(v for v in self._relevant_inputs(var, [s_prog, t_prog]) if v not in assigned)
        if not inputs or cap <= 0:
            return None
        staging = self.services.staging
        for staged in staging.tables(self.run_id, "source"):
            cols = {canonical_name(c): i for i, c in enumerate(staged.columns)}
            if not set(inputs) <= set(cols):
                continue
            combos, truncated = staging.distinct_tuples(staged.artefact_id, [cols[v] for v in inputs], cap)
            evaluated = affected = 0
            weighted: list[tuple[Decimal, int]] = []
            total = Decimal(0)
            for values, n in combos:
                record = dict(zip(inputs, values))
                try:
                    a = simulate(s_prog, record).get(var)
                    b = simulate(t_prog, record).get(var)
                except (ExpressionError, ArithmeticError, ValueError):
                    continue
                if a is None or b is None:
                    continue
                evaluated += n
                if a != b:
                    affected += n
                    if isinstance(a, Decimal) and isinstance(b, Decimal):
                        d = abs(b - a)
                        total += d * n
                        weighted.append((d, n))
            if not evaluated:
                continue
            weighted.sort()
            median = None
            if weighted:
                half, seen = sum(n for _, n in weighted) / 2, 0
                for d, n in weighted:
                    seen += n
                    if seen >= half:
                        median = d
                        break
            return {"extract": staged.path, "records_evaluated": evaluated, "affected": affected,
                    "share": round(affected / evaluated, 4), "sampled": truncated,
                    "distinct_inputs": len(combos),
                    "delta_median": _fmt(median) if median is not None else None,
                    "delta_max": _fmt(weighted[-1][0]) if weighted else None,
                    "delta_total": _fmt(total) if weighted else None, "inputs": inputs}
        return None

    def _relevant_inputs(self, var: str, progs: list[Program]) -> set[str]:
        """Inputs that can influence ``var``: its dependencies, transitively, in either program."""
        seen: set[str] = set()
        stack = [var]
        while stack:
            v = stack.pop()
            for d in self._deps_of(v, progs):
                if d not in seen:
                    seen.add(d)
                    stack.append(d)
        return seen

    @staticmethod
    def _example(divergent: list[dict], relevant: set[str]) -> dict | None:
        """The divergent vector with the median delta, showing only inputs that matter."""
        if not divergent:
            return None

        def delta(d: dict) -> Decimal:
            return abs((_dec(d["target"]) or Decimal(0)) - (_dec(d["source"]) or Decimal(0)))
        ranked = sorted(divergent, key=delta)
        median = delta(ranked[len(ranked) // 2])
        pick = max((d for d in ranked if delta(d) == median), key=lambda d: _dec(d["source"]) or Decimal(0))
        return {**pick, "inputs": {k: v for k, v in pick["inputs"].items() if k in relevant}}

    @staticmethod
    def _deps_of(var: str, progs: list[Program]) -> set[str]:
        out: set[str] = set()
        for p in progs:
            for s in p.steps_for(var):
                try:
                    out |= identifiers(s.expr)
                    for c in s.conditions:
                        out |= identifiers(c)
                except ExpressionError:
                    pass
        return out - {var}

    def _roots(self, missing: list[str], progs: list[Program]) -> list[str]:
        """Missing variables not used by any other missing variable (dependencies fold into their root)."""
        roots = []
        for v in missing:
            used_by_missing = any(v in self._deps_of(m, progs) for m in missing if m != v)
            if not used_by_missing:
                roots.append(v)
        return roots

    @staticmethod
    def _related_docs(var: str, rules: list[NormalisedRule], docs) -> list[NormalisedRule]:
        names = {var} | {v for r in rules for v in r.variables}
        return [d for n in sorted(names) for d in docs.get(n, [])]

    def _explain_with_llm(self, diffs: list[Differential]) -> None:
        """A two-sentence plain-English summary for the most severe differences, through ``narrate`` so it is
        grounded (no figure that is not in the proven facts) and attributed; the proof itself is unchanged."""
        cap = int((self.services.settings.heuristics.get("differential") or {}).get("explain_max", 10))
        for d in sorted(diffs, key=lambda x: -x.severity.score)[:cap]:
            divergence = "; ".join(f"{v.inputs} -> {v.source_value} vs {v.target_value}" for v in d.divergence[:3])
            facts = "\n".join([d.explanation, f"source: {d.source_summary}", f"target: {d.target_summary}",
                               f"divergence: {divergence}"])
            draft, _meta = self.narrate("explain_difference", {
                "rule": d.canonical_name, "source_system": d.source_system or "-", "target_system": d.target_system or "-",
                "source": d.source_summary or "-", "target": d.target_summary or "-", "divergence": divergence or "-"},
                facts, Explanation, text_of=lambda x: x.explanation, content_class=ContentClass.GUARD_INPUT)
            if draft is not None:
                d.explanation = f"{d.explanation} Summary: {draft.explanation[:600]}"
                d.explanation_mode = "llm"


def _count(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out
