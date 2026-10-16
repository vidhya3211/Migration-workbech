"""Anomaly scoring for data-quality cohorts."""
from __future__ import annotations

from reasoning.heuristics.base import make_result
from schemas.reasoning import HeuristicResult

VERSION = "anomaly-1.1"
DEFECT_SEVERITY = {"null_mandatory": 0.9, "invalid_code": 0.85, "invalid_date": 0.8, "out_of_range": 0.75,
                   "duplicate_key": 0.95, "sentinel_value": 0.7, "type_violation": 0.8,
                   "default_value_spike": 0.8, "mixed_null_conventions": 0.6, "orphan_reference": 0.9,
                   "era_correlated_nulls": 0.6, "flagged_population": 0.7}


def score(candidate: str, defect: str, affected: int, total: int, backed_by_certified_rule: bool,
          threshold: float = 0.4) -> HeuristicResult:
    share = affected / total if total else 0.0
    comps = {"defect_severity": DEFECT_SEVERITY.get(defect, 0.5), "prevalence": min(1.0, share * 5),
             "rule_backed": 1.0 if backed_by_certified_rule else 0.5}
    return make_result("anomaly.cohort", VERSION, candidate, comps,
                       {"defect_severity": 0.5, "prevalence": 0.2, "rule_backed": 0.3}, threshold,
                       [f"{affected}/{total} records", "definition from certified rule/schema" if backed_by_certified_rule
                        else "definition from profile only"], f"{defect} cohort")
