"""Break-cluster heuristics: how well does a differential explain a cluster of reconciliation breaks?"""
from __future__ import annotations

from decimal import Decimal

from reasoning.heuristics.base import make_result
from schemas.reasoning import HeuristicResult

VERSION = "break-cluster-1.1"


def link_score(cluster_sig: str, field_name: str, delta: Decimal | None, differential: dict,
               threshold: float = 0.6) -> HeuristicResult:
    """Scores the hypothesis 'this differential caused these breaks'."""
    name = differential.get("canonical_name", "")
    diff_deltas = [Decimal(d) for d in differential.get("deltas", []) if d is not None]
    field_hit = 1.0 if name and (name in field_name.upper() or field_name.upper() in name) else 0.0
    related = differential.get("related_fields", [])
    if not field_hit and any(r in field_name.upper() for r in related):
        field_hit = 0.7
    delta_hit = 0.0
    if delta is not None and diff_deltas:
        delta_hit = 1.0 if any(abs(abs(delta) - abs(d)) <= Decimal("0.005") for d in diff_deltas) else 0.2
    vector_hit = float(differential.get("vector_match", 0.0))
    comps = {"field": field_hit, "delta": delta_hit, "input_region": vector_hit}
    return make_result("break_cluster.link", VERSION, f"{cluster_sig}<-{differential.get('differential_id')}", comps,
                       {"field": 0.3, "delta": 0.45, "input_region": 0.25}, threshold,
                       [f"differential {name} deltas {[str(d) for d in diff_deltas]}", f"cluster delta {delta}"],
                       "differential explains break cluster")
