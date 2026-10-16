"""Scoring helpers shared by ATS policies."""
from __future__ import annotations


def mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def evidence_weighted(score: float, evidence_count: int, saturation: int = 3) -> float:
    """Confidence grows with independent evidence and saturates - more evidence cannot exceed the score."""
    return round(score * min(1.0, (evidence_count + 1) / (saturation + 1)), 4)


def margin(best: float, runner_up: float | None) -> float:
    return round(best - (runner_up or 0.0), 4)
