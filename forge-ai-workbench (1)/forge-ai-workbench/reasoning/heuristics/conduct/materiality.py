"""Conduct materiality (UC12): grades customer impact for routing. A conduct officer decides remediation."""
from __future__ import annotations

from decimal import Decimal

from reasoning.heuristics.base import make_result
from schemas.reasoning import HeuristicResult

VERSION = "conduct-materiality-1.0"
IMPACT_WEIGHT = {"overcharge": 1.0, "benefit_reduction": 1.0, "missing_record": 0.9, "underpayment": 0.8}


def band_for(annual: Decimal, bands: list[dict]) -> str:
    chosen = bands[0]["band"]
    for b in sorted(bands, key=lambda x: x["min"]):
        if annual >= Decimal(str(b["min"])):
            chosen = b["band"]
    return chosen


def score(case_ref: str, impact_type: str, annual_detriment: Decimal, vulnerable: bool, bands: list[dict],
          threshold: float = 0.3) -> tuple[HeuristicResult, str]:
    band = band_for(annual_detriment, bands)
    amount = float(min(Decimal(1), annual_detriment / Decimal(250)))
    comps = {"impact": IMPACT_WEIGHT.get(impact_type, 0.5), "amount": round(amount, 4),
             "vulnerability": 1.0 if vulnerable else 0.0}
    res = make_result("conduct.materiality", VERSION, case_ref, comps,
                      {"impact": 0.35, "amount": 0.5, "vulnerability": 0.15}, threshold,
                      [f"annualised detriment {annual_detriment}", f"band {band}"], f"{impact_type} materiality")
    return res, band
