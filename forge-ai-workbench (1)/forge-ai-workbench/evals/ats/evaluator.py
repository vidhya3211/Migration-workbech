"""ATS evaluation: correct-candidate discovery, search efficiency, coverage, unnecessary branches, depth, latency."""
from __future__ import annotations

import time
from decimal import Decimal

from db import models as m
from evals.harness import CapabilityResult, Workspace
from reasoning.ats.budget import ATSBudget
from reasoning.ats.engine import ATSEngine
from reasoning.ats.policies import EntityClusterPolicy, MappingAssignmentPolicy, ReplayAttributionPolicy, RootCausePolicy
from schemas.reasoning import HeuristicResult


def _h(s: str, t: str, score: float) -> HeuristicResult:
    return HeuristicResult(heuristic="eval", candidate=f"{s}->{t}", score=score, score_components={}, threshold=0.5,
                           passed=score >= 0.5, reason="eval", algorithm_version="eval")


def _cases(ws: Workspace | None) -> list[tuple[str, object, object]]:
    catalogue = [c.model_dump() for c in (ws.settings.root_causes if ws else [])] or [
        {"code": "RC-LEGACY-DEFAULT", "signatures": ["sentinel_value"], "description": ""},
        {"code": "RC-DATE-FORMAT", "signatures": ["invalid_date"], "description": ""}]
    values = {frozenset(): Decimal("41.88"), frozenset({"ADMIN_FEE"}): Decimal("36.88"),
              frozenset({"RISK_FACTOR"}): Decimal("43.20"), frozenset({"ADMIN_FEE", "RISK_FACTOR"}): Decimal("38.20")}
    return [
        ("mapping_contention",
         MappingAssignmentPolicy(["CUST_ID", "POL_NUMBER", "CUST_NAME"], {
             "CUST_ID": [_h("CUST_ID", "party_id", .8), _h("CUST_ID", "policy_ref", .3)],
             "POL_NUMBER": [_h("POL_NUMBER", "policy_ref", .9), _h("POL_NUMBER", "party_id", .4)],
             "CUST_NAME": [_h("CUST_NAME", "party_id", .6), _h("CUST_NAME", "full_name", .58)]}),
         lambda best: best.state["assigned"] == {"CUST_ID": "party_id", "POL_NUMBER": "policy_ref",
                                                 "CUST_NAME": "full_name"}),
        ("root_cause_sentinel", RootCausePolicy("sentinel_value", {"sentinel_values": ["00000000"]}, catalogue),
         lambda best: best.hypothesis == "RC-LEGACY-DEFAULT"),
        ("entity_non_transitive", EntityClusterPolicy(["a", "b", "c"], {("a", "b"): .95, ("b", "c"): .9, ("a", "c"): .5}, .85),
         lambda best: sorted(map(sorted, best.state["clusters"])) in ([["a", "b"], ["c"]], [["a"], ["b", "c"]])),
        ("replay_single_cause", ReplayAttributionPolicy(["ADMIN_FEE", "RISK_FACTOR"],
                                                        lambda t: values.get(frozenset(t)), Decimal("36.88")),
         lambda best: best.state["toggled"] == ["ADMIN_FEE"]),
    ]


def evaluate(ws: Workspace | None) -> CapabilityResult:
    found = branches = expanded = pruned = depth = 0
    latency = []
    details = {}
    cases = _cases(ws)
    for name, policy, check in cases:
        t0 = time.perf_counter()
        res = ATSEngine(ATSBudget()).search(policy)
        latency.append(time.perf_counter() - t0)
        ok = res.best is not None and check(res.best)
        found += ok
        branches += res.branches
        expanded += res.expanded
        pruned += res.pruned
        depth = max(depth, res.max_depth_reached)
        details[name] = {"ok": ok, **res.summary()}
    if ws is not None and ws.run_id:   # the pipeline's own ATS runs must have produced proven replay attributions
        clusters = ws.services.kc.rows(m.BreakCluster, ws.run_id)
        details["pipeline_replay_explained"] = sum(1 for c in clusters if "reproduces the source value" in c.body["hypothesis"])
    return CapabilityResult("ats", {
        "discovery_rate": round(found / len(cases), 4),
        "branches_per_case": round(branches / len(cases), 2),
        "unnecessary_branch_ratio": round(pruned / max(1, branches), 4),
        "max_depth": float(depth),
        "mean_latency_ms": round(1000 * sum(latency) / len(latency), 2),
        "coverage": round(expanded / max(1, branches), 4),
    }, details)

