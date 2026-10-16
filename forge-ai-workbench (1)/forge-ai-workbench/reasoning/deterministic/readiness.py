"""Cutover readiness score (UC15): deterministic, explainable, and never a go/no-go decision by itself.

The score summarises; the Migration Lead decides.
"""
from __future__ import annotations

from dataclasses import dataclass, field

WEIGHTS = {
    "rules_certified": 0.25,
    "differentials_resolved": 0.20,
    "mappings_certified": 0.15,
    "cohorts_addressed": 0.10,
    "breaks_explained": 0.15,
    "conduct_cases_decided": 0.10,
    "assurance_passed": 0.05,
}


@dataclass
class Readiness:
    score: float
    components: dict[str, float]
    blockers: list[str] = field(default_factory=list)
    version: str = "readiness-1.0"


def ratio(done: int, total: int) -> float:
    return 1.0 if total == 0 else round(done / total, 4)


def compute(counts: dict[str, tuple[int, int]], assurance_passed: bool) -> Readiness:
    """``counts``: component -> (done, total)."""
    comps = {k: ratio(*counts.get(k, (0, 0))) for k in WEIGHTS if k != "assurance_passed"}
    comps["assurance_passed"] = 1.0 if assurance_passed else 0.0
    score = round(sum(WEIGHTS[k] * v for k, v in comps.items()), 4)
    blockers = [f"{k.replace('_', ' ')}: {counts.get(k, (0, 0))[0]}/{counts.get(k, (0, 0))[1]}"
                for k, v in comps.items() if v < 1.0 and k != "assurance_passed"]
    if not assurance_passed:
        blockers.append("assurance gate not passed")
    return Readiness(score, comps, blockers)
