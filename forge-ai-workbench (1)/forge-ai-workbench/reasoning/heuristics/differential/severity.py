"""Differential severity: ranks disagreements by financial exposure (UC5). Ranking only - not a decision.

What an adjudicator reads first is a client setting, not an engineering constant: domain and type weights, the
component weights and the population scale come from ``heuristics.differential.severity`` per project. Changing
them re-orders the register; it never changes a finding. The population term is logarithmic in the number of
real records affected (from the real-book evaluation), so ten thousand affected policies outrank ten.
"""
from __future__ import annotations

import math

from decimal import Decimal

from reasoning.heuristics.base import make_result
from schemas.reasoning import HeuristicResult

VERSION = "differential-severity-2.0"
DOMAIN_WEIGHT = {"premium": 1.0, "charges": 1.0, "surrender": 1.0, "bonus": 0.9, "underwriting": 0.85,
                 "policy_status": 0.7, "general": 0.5}
TYPE_WEIGHT = {"value_mismatch": 0.9, "expression_mismatch": 1.0, "condition_mismatch": 0.8,
               "missing_in_target": 0.9, "missing_in_source": 0.6, "document_conflict": 0.6, "rounding": 0.2}


def score(candidate: str, domain: str, difference_type: str, max_delta: Decimal | None, reference: Decimal | None,
          divergent_share: float, affected_records: int | None, threshold: float = 0.3,
          cfg: dict | None = None) -> HeuristicResult:
    cfg = cfg or {}
    domain_w = {**DOMAIN_WEIGHT, **(cfg.get("domain_weights") or {})}
    type_w = {**TYPE_WEIGHT, **(cfg.get("type_weights") or {})}
    rel = 0.0
    if max_delta is not None:
        base = abs(reference) if reference not in (None, Decimal(0)) else Decimal(1)
        rel = float(min(Decimal(1), max_delta / base))
    comps = {
        "domain": domain_w.get(domain, domain_w.get("_default", 0.5)),
        "type": type_w.get(difference_type, 0.5),
        "relative_delta": round(rel, 4) if max_delta is not None else 0.5,
        "input_share": round(divergent_share, 4),
    }
    weights = {"domain": 0.3, "type": 0.25, "relative_delta": 0.3, "input_share": 0.15, "records": 0.2,
               **(cfg.get("component_weights") or {})}
    if affected_records is not None:
        scale = float(cfg.get("population_scale", 4))          # log10 records at which the term saturates
        comps["records"] = round(min(1.0, math.log10(1 + affected_records) / scale), 4)
    else:
        weights.pop("records")
    evidence = [f"max delta {max_delta}", f"divergent share {divergent_share:.2f}"]
    if affected_records is not None:
        evidence.append(f"{affected_records} real source record(s) get a different result from the two rules")
    return make_result("differential.severity", VERSION, candidate, comps, weights, threshold, evidence,
                       "financial exposure ranking")
