"""Guardrail evaluation: false-negative (missed attack / leaked PII) and false-positive rates."""
from __future__ import annotations

from evals.harness import CapabilityResult, dataset
from guardrails.injection.detector import InjectionPolicy
from guardrails.pii.detector import find_pii
from guardrails.pipeline import GuardrailContext, Stage


def evaluate(_ws=None) -> CapabilityResult:
    gold = dataset("guardrails_golden")
    policy = InjectionPolicy(0.6)
    ctx = GuardrailContext("EVAL", "eval")
    fn = fp = pos = neg = 0
    misses = []
    for case in gold["injection"]:
        blocked = not policy.check(case["text"], ctx, Stage.INPUT).passed
        if case["label"] == "block":
            pos += 1
            fn += not blocked
            if not blocked:
                misses.append(case["text"])
        else:
            neg += 1
            fp += blocked
    pfn = pfp = ppos = pneg = 0
    for case in gold["pii"]:
        found = bool(find_pii(case["text"]))
        if case["label"] == "block":
            ppos += 1
            pfn += not found
        else:
            pneg += 1
            pfp += found
    return CapabilityResult("guardrails", {
        "injection_false_negative_rate": round(fn / pos, 4), "injection_false_positive_rate": round(fp / neg, 4),
        "pii_false_negative_rate": round(pfn / ppos, 4), "pii_false_positive_rate": round(pfp / pneg, 4),
        "false_negative_rate": round((fn + pfn) / (pos + ppos), 4),
    }, {"missed_injections": misses})
