"""Heuristic evaluation: ranking quality (top-1), candidate recall, threshold precision, false ranking rate."""
from __future__ import annotations

from evals.harness import CapabilityResult, dataset
from reasoning.heuristics.duplicate.scoring import score as dup
from reasoning.heuristics.naming_match import score as name_match


def evaluate(_ws=None) -> CapabilityResult:
    gold = dataset("heuristics_golden")
    top1 = recall = false_rank = 0
    for case in gold["name_match"]:
        scored = sorted((name_match(case["phrase"], c, threshold=0.7) for c in case["candidates"]), key=lambda h: -h.score)
        best = scored[0]
        if case["expected"] == "none":
            ok = not best.passed
        else:
            ok = best.passed and best.candidate == case["expected"]
            recall += case["expected"] in [h.candidate for h in scored if h.passed]
        top1 += ok
        false_rank += (not ok)
    tp = fp = fn = tn = 0
    for case in gold["duplicates"]:
        h = dup("L", "R", case["left"], case["right"], threshold=0.85)
        tp += h.passed and case["match"]
        fp += h.passed and not case["match"]
        fn += (not h.passed) and case["match"]
        tn += (not h.passed) and not case["match"]
    n = len(gold["name_match"])
    positives = sum(1 for c in gold["name_match"] if c["expected"] != "none")
    return CapabilityResult("heuristics", {
        "top1": round(top1 / n, 4),
        "candidate_recall": round(recall / positives, 4),
        "false_ranking_rate": round(false_rank / n, 4),
        "duplicate_threshold_precision": round(tp / (tp + fp), 4) if tp + fp else 0.0,
        "duplicate_recall": round(tp / (tp + fn), 4) if tp + fn else 0.0,
    }, {"duplicates": {"tp": tp, "fp": fp, "fn": fn, "tn": tn}})
