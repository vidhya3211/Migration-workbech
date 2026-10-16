"""Break-cluster agent (T2): groups mismatches by shared root cause (UC11).

For each cluster the agent *replays* a representative record deterministically:
  1. run the record's source inputs through the target logic - does it reproduce the loaded target value?
  2. ATS searches the smallest set of certified differentials whose *source* definition, swapped into the
     target logic, reproduces the source value exactly - that set is the proven cause.
Clusters no replay can explain fall back to the link heuristic and are flagged for investigation.
The reconciliation analyst decides whether each cluster is acceptable variance or a real problem.

Breaks are grouped by PostgreSQL (``GROUP BY`` signature over ``reconciliation_break``) - a cluster carries its
true count and total and a capped sample of break ids; membership is the ``cluster_id`` column, so a million
breaks cost one row per signature. The representative's source record is looked up by key in the staged extract.
"""
from __future__ import annotations

from decimal import Decimal

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from pydantic import BaseModel, Field

from agents.base import AgentOutput, BaseAgent
from agents.data_quality.extracts import load_extracts
from reasoning.ats.policies import BreakCausePolicy, ReplayAttributionPolicy
from reasoning.deterministic.equivalence import Program, Step, simulate
from reasoning.deterministic.expressions import ExpressionError
from reasoning.deterministic.naming import canonical_name
from reasoning.deterministic.programs import build_programs, dependents
from reasoning.heuristics.base import make_result
from reasoning.heuristics.break_cluster.scoring import link_score
from schemas.common import Tier, ReasoningMode, stable_id
from schemas.differential import Differential
from schemas.mapping import DictionaryEntry
from schemas.recon import BreakCluster, ReconciliationBreak
from schemas.rules import NormalisedRule
from schemas.signals import SignalType


class BreakHypothesis(BaseModel):
    hypothesis: str
    checks: list[str] = Field(default_factory=list)


def cluster_signature(g: dict) -> str:
    """Stable signature of a break group (exact delta, systematic relative shift, or relative band)."""
    base = f"{g['entity']}.{g['field']}:{g['break_type']}"
    kind = g.get("kind", "exact")
    if kind == "shift":
        return f"{base}:shift{'+' if g['sign'] > 0 else '-'}"
    if kind == "band":
        return f"{base}:band:{g['band']}"
    return base + (f":{g['delta']}" if g["delta"] is not None else "")


def shape_text(shape: dict) -> str:
    if shape.get("kind") == "shift":
        return (f"Systematic shift of {shape['median_shift'] * 100:+.2f}% of the expected value across "
                f"{shape['cohort']} breaks ({shape['distinct_deltas']} different amounts, dispersion "
                f"{shape['dispersion']:.2f}) - one cause, not {shape['cohort']} separate errors.")
    if shape.get("kind") == "band":
        return f"Relative band '{shape['band']}' (too many distinct amounts to list individually)."
    return ""


def hybrid(target: Program, source: Program, toggled: frozenset[str]) -> Program:
    """Target logic with the source definition of each toggled variable swapped in, at the same point."""
    steps: list[Step] = []
    inserted: set[str] = set()
    for s in target.steps:
        if s.target in toggled:
            if s.target not in inserted:
                steps.extend(source.steps_for(s.target))
                inserted.add(s.target)
            continue
        steps.append(s)
    missing = [v for v in sorted(toggled) if v not in inserted]
    prefix = [s for v in missing for s in source.steps_for(v)]
    return Program(key=f"{target.key}+{'+'.join(sorted(toggled))}", label="hybrid", role="target", steps=prefix + steps)


@register_agent
class BreakClusterAgent(BaseAgent):
    IDENTITY = agent_identity("break_cluster_agent", Tier.T2, "reconciliation", 'Groups mismatches by shared root cause', {S.BREAK_CLUSTERS, S.HUMAN_REVIEW_REQUIRED}, ats=True, llm={'break_hypothesis'})

    def run(self, differentials: list[Differential], normalised: list[NormalisedRule],
            dictionary: list[DictionaryEntry], duplicates: dict[str, str] | None = None,
            breaks: list[ReconciliationBreak] | None = None, **_: object) -> AgentOutput:
        self.duplicates = duplicates or {}
        kc = self.services.kc
        sample_n = self.services.settings.scale.cohort_record_refs
        programs = build_programs(normalised)
        src_x = {e.entity: e for e in load_extracts(self.services, self.run_id, "source", dictionary) if e.entity}
        clusters: list[BreakCluster] = []
        total_breaks = 0
        for g in kc.break_groups(self.run_id, self.services.settings.heuristics.get("break_cluster") or {}):
            sig = cluster_signature(g)
            first = kc.get_break(g["first_break_id"])
            delta = Decimal(first.delta) if first.delta is not None else None
            hypothesis, linked, score = self._explain(first, differentials, programs, src_x, delta)
            model_hypothesis = None
            if not linked and hypothesis.startswith("Not explained"):
                facts = (f"signature {sig}; breaks {g['count']}; total delta {g['total_delta']}; example: field "
                         f"{first.field}, source {first.source_value}, expected {first.expected_value}, loaded "
                         f"{first.target_value}, delta {first.delta}")
                draft, meta = self.narrate("break_hypothesis", {"signature": sig, "facts": facts}, facts,
                                           BreakHypothesis, text_of=lambda p: " ".join([p.hypothesis, *p.checks]))
                if draft is not None:
                    model_hypothesis = {**draft.model_dump(), **(meta or {}), "status": "unproven"}
            if shape_text(g.get("shape") or {}):
                hypothesis = f"{shape_text(g['shape'])} {hypothesis}"
            total_breaks += g["count"]
            clusters.append(BreakCluster(
                cluster_id=stable_id("CLU", self.run_id, sig), run_id=self.run_id, signature=sig, field=first.field,
                break_ids=kc.break_ids(self.run_id, g, limit=sample_n), count=g["count"],
                total_delta=format(g["total_delta"], "f"), hypothesis=hypothesis,
                linked_differential_id=linked[0] if linked else None,
                linked_rule=", ".join(d.canonical_name for d in differentials if d.differential_id in linked) or None,
                score=score, model_hypothesis=model_hypothesis, shape=g.get("shape") or {"kind": "exact"}))
        self.signal(SignalType.BREAK_CLUSTERS, f"{total_breaks} break(s) grouped into {len(clusters)} cluster(s)",
                    item_refs=[c.cluster_id for c in clusters],
                    data={"explained": sum(1 for c in clusters if c.linked_differential_id),
                          "clusters": [f"{c.signature} x{c.count} -> {c.linked_rule or c.hypothesis[:60]}" for c in clusters[:10]]})
        return AgentOutput(items={"clusters": clusters})

    def _explain(self, b: ReconciliationBreak, diffs: list[Differential], programs: dict[str, Program], src_x, delta):
        if b.break_type == "missing_in_target" and b.record_key in self.duplicates:
            h = make_result("break_cluster.link", "break-cluster-1.1", "duplicate", {"steward_confirmed": 1.0},
                            {"steward_confirmed": 1.0}, 0.5, [f"{b.record_key} confirmed duplicate of "
                                                            f"{self.duplicates[b.record_key]}"], "merged duplicate")
            return (f"Record not loaded: a data steward confirmed {b.record_key} is a duplicate of "
                    f"{self.duplicates[b.record_key]}, so it was merged rather than migrated."), [], h
        if b.break_type == "rejected_at_load":
            h = make_result("break_cluster.link", "break-cluster-1.1", "rejected", {"load_report": 1.0},
                            {"load_report": 1.0}, 0.5, [f"{b.record_key} listed in the target load-rejection report"],
                            "rejected at load")
            return (f"Record not loaded: the target load rejected it ({b.note or 'no reason given'}). Fix the record "
                    "or the load rule, then re-load."), [], h
        if b.break_type == "missing_in_target":
            res = self.ats(BreakCausePolicy({"signature": b.field, "type": b.break_type, "uniform_delta": True}, []))
            h = make_result("break_cluster.link", "break-cluster-1.1", "missing", {"type": 1.0}, {"type": 1.0}, 0.5,
                            [f"record {b.record_key} not present in the target extract"], "record not loaded")
            return (f"Record not loaded into the target (ATS: {res.best.hypothesis if res.best else 'unresolved'}).",
                    [], h)
        var = canonical_name(b.field)
        tgt = next((p for p in programs.values() if p.role == "target" and var in p.assigned()), None)
        src = next((p for p in programs.values() if p.role == "source" and var in p.assigned()
                    and not p.label.lower().endswith((".xlsx", ".xlsm"))), None)
        record = self._source_record(b, src_x)
        if tgt is not None and src is not None and record is not None and b.source_value is not None:
            self.note_mode(ReasoningMode.DETERMINISTIC)
            relevant = {var} | {v for v in (src.assigned() | tgt.assigned()) if var in dependents([src, tgt], v)}
            candidates = sorted({d.canonical_name for d in diffs if d.canonical_name in relevant},
                                key=lambda n: -max(d.severity.score for d in diffs if d.canonical_name == n))
            inputs = {canonical_name(k): v for k, v in record.items()}
            inputs = {k: v for k, v in inputs.items() if k not in (src.assigned() | tgt.assigned())}

            def replay(toggled: frozenset[str]) -> Decimal | None:
                try:
                    v = simulate(hybrid(tgt, src, toggled), inputs).get(var)
                    return v if isinstance(v, Decimal) else None
                except ExpressionError:
                    return None
            base = replay(frozenset())
            try:
                reproduces = base is not None and Decimal(b.target_value or "x") == base
            except Exception:
                reproduces = False
            res = self.ats(ReplayAttributionPolicy(candidates, replay, Decimal(b.source_value)))
            if res.best is not None:
                names = res.best.state["toggled"]
                linked = [d.differential_id for d in diffs if d.canonical_name in names]
                score = make_result("break_cluster.replay", "replay-1.0", "+".join(names),
                                    {"reproduces_target": 1.0 if reproduces else 0.0, "reproduces_source": 1.0},
                                    {"reproduces_target": 0.3, "reproduces_source": 0.7}, 0.6, res.best.evidence,
                                    "deterministic replay attribution")
                text = (f"Explained by certified differential(s) {', '.join(names)}: replaying record {b.record_key} "
                        f"through the target logic {'reproduces the loaded value ' + str(base) if reproduces else 'gives ' + str(base)}"
                        f"; swapping in the source rule(s) reproduces the source value {b.source_value} exactly "
                        f"(ATS {res.ats_run_id}).")
                return text, linked, score
        # fallback: heuristic linking (no executable replay available)
        links = [link_score(b.field, b.field, delta, {"differential_id": d.differential_id,
                                                      "canonical_name": d.canonical_name,
                                                      "deltas": [v.target_value and v.source_value and
                                                                 str(abs(Decimal(v.target_value) - Decimal(v.source_value)))
                                                                 for v in d.divergence if v.target_value and v.source_value],
                                                      "related_fields": []}) for d in diffs]
        res = self.ats(BreakCausePolicy({"signature": b.field, "type": b.break_type, "uniform_delta": delta is not None},
                                        links))
        if res.best is not None and res.best.state["cause"] not in ("load_defect", "missing"):
            did = res.best.state["cause"]
            h = next(h for h in links if h.candidate.endswith(did))
            return f"Probably caused by differential {did} (heuristic link, not replay-proven).", [did], h
        h = make_result("break_cluster.link", "break-cluster-1.1", b.field, {"explained": 0.0}, {"explained": 1.0}, 0.6,
                        ["no certified differential reproduces this break"], "unexplained")
        return ("Not explained by any certified differential - investigate the transformation or the load "
                f"(value {b.expected_value} expected, {b.target_value} loaded)."), [], h

    @staticmethod
    def _source_record(b: ReconciliationBreak, src_x) -> dict | None:
        for ex in src_x.values():
            if ex.key is None:
                continue
            found = ex.find(ex.key, b.record_key, limit=1)
            if found:
                _, row = found[0]
                return {h: ex.value(row, h) for h in ex.header}
        return None
