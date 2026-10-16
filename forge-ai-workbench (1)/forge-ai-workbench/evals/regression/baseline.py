"""Regression evaluation: compare every metric with the accepted baseline; a material drop blocks release."""
from __future__ import annotations

import json
from pathlib import Path

BASELINE = Path(__file__).resolve().parent.parent / "reports" / "baseline.json"
LOWER_IS_BETTER = {"unsupported_claim_rate", "false_negative_rate", "injection_false_negative_rate",
                   "injection_false_positive_rate", "pii_false_negative_rate", "pii_false_positive_rate",
                   "missed_breaks", "false_breaks", "false_positives", "false_negatives", "quality_drop",
                   "false_ranking_rate", "unnecessary_branch_ratio", "branches_per_case", "mean_latency_ms",
                   "latency_ms", "tokens_after", "max_depth"}
INFORMATIONAL = {"rules_extracted", "tokens_before", "tokens_after", "latency_ms", "mean_latency_ms", "max_depth",
                 "branches_per_case", "boundary_detection", "compression_ratio"}
TOLERANCE = 0.01


def compare(current: dict[str, dict[str, float]]) -> list[str]:
    if not BASELINE.exists():
        return []
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    out = []
    for cap, metrics in current.items():
        for name, value in metrics.items():
            old = base.get(cap, {}).get(name)
            if old is None or name in INFORMATIONAL:
                continue
            worse = value > old + TOLERANCE if name in LOWER_IS_BETTER else value < old - TOLERANCE
            if worse:
                out.append(f"{cap}.{name}: {old} -> {value}")
    return out


def write(current: dict[str, dict[str, float]]) -> None:
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps(current, indent=1, sort_keys=True), encoding="utf-8")
