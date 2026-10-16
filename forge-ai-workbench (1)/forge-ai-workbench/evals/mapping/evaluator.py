"""Mapping evaluation: mapping accuracy, candidate recall, false positives / negatives, transformation accuracy."""
from __future__ import annotations

from db import models as m
from evals.harness import CapabilityResult, Workspace, dataset


def evaluate(ws: Workspace) -> CapabilityResult:
    gold = dataset("mapping_golden")
    rows = ws.services.kc.rows(m.Mapping, ws.run_id)
    got = {(r.source_entity, r.source_field): r for r in rows}
    exp = {(e["source_entity"], e["source_field"]): e for e in gold["expected"]}
    correct = [k for k, e in exp.items() if k in got and (got[k].target_entity, got[k].target_field) ==
               (e["target_entity"], e["target_field"])]
    transform_ok = [k for k in correct if got[k].transformation.get("kind") == exp[k]["transformation"]]
    candidate_recall = [k for k, e in exp.items() if k in got and (
        (got[k].target_entity, got[k].target_field) == (e["target_entity"], e["target_field"])
        or f"{e['target_entity']}.{e['target_field']}" in " ".join(got[k].body.get("alternatives", [])))]
    fp = [k for k in got if k not in exp or k not in correct]
    unmapped_ok = all(tuple(u.split(".", 1)) not in got for u in gold["unmapped"])
    return CapabilityResult("mapping", {
        "accuracy": round(len(correct) / len(exp), 4),
        "transformation_accuracy": round(len(transform_ok) / max(1, len(correct)), 4),
        "candidate_recall": round(len(candidate_recall) / len(exp), 4),
        "false_positives": float(len(fp)),
        "false_negatives": float(len(exp) - len(correct)),
        "unmapped_respected": float(unmapped_ok),
    }, {"wrong": [f"{k[0]}.{k[1]}" for k in fp]})
