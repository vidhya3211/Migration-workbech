"""Confidence aggregation: combines independent evidence signals into one explainable confidence."""
from __future__ import annotations

from reasoning.heuristics.base import make_result
from schemas.reasoning import HeuristicResult

VERSION = "confidence-1.0"


def noisy_or(values: list[float]) -> float:
    p = 0.0
    for v in values:
        p = p + max(0.0, min(1.0, v)) * (1 - p)
    return round(p, 4)


def aggregate(candidate: str, signals: dict[str, float], threshold: float = 0.5,
              penalties: dict[str, float] | None = None) -> HeuristicResult:
    """Positive signals combine by noisy-OR; penalties (0..1) scale the result down multiplicatively."""
    base = noisy_or(list(signals.values()))
    score = base
    for v in (penalties or {}).values():
        score *= (1 - v)
    comps = {**{f"+{k}": v for k, v in signals.items()}, **{f"-{k}": v for k, v in (penalties or {}).items()}}
    return make_result("confidence.aggregate", VERSION, candidate, comps, {}, threshold,
                       [f"noisy-or {base:.3f}"], "aggregated confidence", score=score)
