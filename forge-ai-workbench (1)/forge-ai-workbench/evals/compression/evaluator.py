"""Headroom compression-vs-quality evaluation (§20, Phase 7).

A compression configuration is rejected if it materially degrades quality: critical facts must remain
visible to the model, protected evidence must be byte-identical, and everything compressed must be
retrievable from the context store.
"""
from __future__ import annotations

import time

from context.headroom.optimizer import ContextBlock, HeadroomOptimizer
from context.headroom.policy import ContentClass
from evals.harness import CapabilityResult, Workspace, dataset


def evaluate(ws: Workspace) -> CapabilityResult:
    gold = dataset("compression_golden")
    opt: HeadroomOptimizer = ws.services.headroom
    before = after = facts = visible = retrievable = protected_ok = protected = 0
    latency = 0.0
    per = {}
    for p in gold["payloads"]:
        t0 = time.perf_counter()
        res = opt.optimize("EVAL", [ContextBlock(p["id"], "user", p["content"], ContentClass(p["content_class"]))])
        latency += time.perf_counter() - t0
        metric = res.metrics[0]
        text = res.messages[0]["content"]
        before += metric.tokens_before
        after += metric.tokens_after
        for f in p["critical_facts"]:
            facts += 1
            visible += f in text
            if metric.original_reference:
                retrievable += f in opt.retrieve(metric.original_reference)
            else:
                retrievable += f in text
        if p.get("protected"):
            protected += 1
            protected_ok += text == p["content"] and metric.compression_mode == "pass_through"
        per[p["id"]] = {"mode": metric.compression_mode, "before": metric.tokens_before, "after": metric.tokens_after,
                        "ratio": metric.compression_ratio, "transforms": metric.transforms}
    preservation = visible / facts
    return CapabilityResult("compression", {
        "tokens_before": float(before), "tokens_after": float(after),
        "compression_ratio": round((before - after) / before, 4) if before else 0.0,
        "evidence_preservation": round(preservation, 4),
        "retrievable": round(retrievable / facts, 4),
        "protected_integrity": round(protected_ok / max(1, protected), 4),
        "quality_drop": round(1 - preservation, 4),
        "latency_ms": round(latency * 1000, 1),
    }, per)
