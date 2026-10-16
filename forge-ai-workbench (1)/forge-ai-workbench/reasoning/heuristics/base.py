"""Heuristic framework (§5): "how strong is this candidate?"

Heuristics generate and rank candidates. They never certify. Every result exposes candidate, score,
score_components, threshold, evidence, reason and algorithm_version - the logic is in code, measurable
by the evaluation harness, and never hidden in a prompt.
"""
from __future__ import annotations

import re
from threading import Lock

from observability.logging.logger import Event, log_event
from schemas.reasoning import HeuristicResult

_counter_lock = Lock()
_calls = 0


def heuristic_calls() -> int:
    return _calls


def weighted(components: dict[str, float], weights: dict[str, float]) -> float:
    total = sum(weights.get(k, 0) for k in components)
    if total == 0:
        return 0.0
    return round(sum(components[k] * weights.get(k, 0) for k in components) / total, 4)


def make_result(heuristic: str, version: str, candidate: str, components: dict[str, float], weights: dict[str, float],
                threshold: float, evidence: list[str], reason: str, score: float | None = None) -> HeuristicResult:
    global _calls
    s = weighted(components, weights) if score is None else round(score, 4)
    s = min(1.0, max(0.0, s))
    res = HeuristicResult(heuristic=heuristic, candidate=candidate, score=s,
                          score_components={k: round(v, 4) for k, v in components.items()}, threshold=threshold,
                          passed=s >= threshold, evidence=evidence, reason=reason, algorithm_version=version)
    with _counter_lock:
        _calls += 1
    log_event(Event.HEURISTIC_EVALUATED, heuristic=heuristic, score=res.score, passed=res.passed, version=version)
    return res


# ------------------------------------------------------------------ string similarity primitives
def jaro_winkler(a: str, b: str, p: float = 0.1) -> float:
    a, b = a.upper(), b.upper()
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    window = max(len(a), len(b)) // 2 - 1
    am, bm = [False] * len(a), [False] * len(b)
    matches = 0
    for i, ch in enumerate(a):
        lo, hi = max(0, i - window), min(len(b), i + window + 1)
        for j in range(lo, hi):
            if not bm[j] and b[j] == ch:
                am[i] = bm[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    t, k = 0, 0
    for i in range(len(a)):
        if am[i]:
            while not bm[k]:
                k += 1
            if a[i] != b[k]:
                t += 1
            k += 1
    jaro = (matches / len(a) + matches / len(b) + (matches - t / 2) / matches) / 3
    prefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        prefix += 1
    return round(jaro + prefix * p * (1 - jaro), 4)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return round(len(a & b) / len(a | b), 4) if a | b else 0.0


def containment(a: set[str], b: set[str]) -> float:
    """How much of the smaller token set is contained in the larger (ADMIN_FEE in MONTHLY_ADMIN_FEE -> 1.0)."""
    if not a or not b:
        return 0.0
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    return round(len(small & large) / len(small), 4)


def norm_text(s: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", s.upper()).strip()
