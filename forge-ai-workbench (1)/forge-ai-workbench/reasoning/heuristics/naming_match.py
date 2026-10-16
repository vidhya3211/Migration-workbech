"""Name-to-rule matching for the Normaliser: which canonical rule is a document claim / workbook label about?

``monthly administration charge`` (document) -> ADMIN_FEE (code), scored by token containment after
abbreviation/synonym expansion, plus value agreement where both sides are numeric.
"""
from __future__ import annotations

from decimal import Decimal

from reasoning.deterministic.naming import canonical_name, name_tokens
from reasoning.heuristics.base import containment, jaccard, jaro_winkler, make_result
from schemas.reasoning import HeuristicResult

VERSION = "name-match-1.2"
NOISE = {"MONTHLY", "ANNUAL", "FIXED", "TOTAL"}   # qualifiers that do not change *what* the rule is


def score(phrase: str, candidate: str, *, phrase_value: str | None = None, candidate_values: list[str] | None = None,
          threshold: float = 0.6) -> HeuristicResult:
    cp = canonical_name(phrase)
    ta, tb = name_tokens(cp), name_tokens(candidate)
    core_a, core_b = ta - NOISE, tb - NOISE
    small = min(len(core_a or ta), len(core_b or tb))
    comps = {   # containment of a one-word name proves little ("RATE" is inside "BASE RATE")
        "tokens": max(jaccard(ta, tb), containment(core_a or ta, core_b or tb) * (1.0 if small >= 2 else 0.6)),
        "spelling": jaro_winkler(cp.replace("_", ""), candidate.replace("_", "")),
    }
    weights = {"tokens": 0.7, "spelling": 0.3}
    evidence = [f"{cp} vs {candidate}"]
    if phrase_value is not None and candidate_values:
        try:
            pv = Decimal(phrase_value)
            close = any(abs(pv - Decimal(v)) <= abs(pv) * Decimal("0.5") for v in candidate_values)
            comps["value_plausible"] = 1.0 if close else 0.3
            weights["value_plausible"] = 0.2
            evidence.append(f"value {phrase_value} vs {candidate_values}")
        except Exception:
            pass
    return make_result("naming.match", VERSION, candidate, comps, weights, threshold, evidence,
                       "document/workbook name matched to a code rule name")
