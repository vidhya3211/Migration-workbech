"""Evaluation gate (§22): no production deployment below the configured floors. Thresholds live in config."""
from __future__ import annotations

from dataclasses import dataclass

# config key -> (capability, metric, "min"|"max")
GATE_MAP: dict[str, tuple[str, str, str]] = {
    "extraction_precision": ("extraction", "precision", "min"),
    "extraction_recall": ("extraction", "recall", "min"),
    "locator_accuracy": ("extraction", "locator_accuracy", "min"),
    "mapping_accuracy": ("mapping", "accuracy", "min"),
    "heuristic_top1": ("heuristics", "top1", "min"),
    "ats_discovery_rate": ("ats", "discovery_rate", "min"),
    "differential_recall": ("differentials", "critical_recall", "min"),
    "reconciliation_accuracy": ("reconciliation", "numerical_correctness", "min"),
    "provenance_accuracy": ("governance", "provenance_accuracy", "min"),
    "unsupported_claim_rate_max": ("llm", "unsupported_claim_rate", "max"),
    "guardrail_false_negative_rate_max": ("guardrails", "false_negative_rate", "max"),
    "retrieval_recall": ("retrieval", "recall", "min"),
    "assistant_refusal_accuracy": ("assistant", "refusal_accuracy", "min"),
    "compression_quality_drop_max": ("compression", "quality_drop", "max"),
    "trace_completeness": ("governance", "trace_completeness", "min"),
    "audit_completeness": ("governance", "audit_completeness", "min"),
}


@dataclass
class GateCheck:
    key: str
    capability: str
    metric: str
    threshold: float
    value: float | None
    passed: bool


def evaluate_gate(results: dict[str, dict[str, float]], thresholds: dict[str, float]) -> list[GateCheck]:
    checks = []
    for key, threshold in thresholds.items():
        cap, metric, kind = GATE_MAP[key]
        value = results.get(cap, {}).get(metric)
        passed = value is not None and (value >= threshold if kind == "min" else value <= threshold)
        checks.append(GateCheck(key, cap, metric, threshold, value, passed))
    return checks
