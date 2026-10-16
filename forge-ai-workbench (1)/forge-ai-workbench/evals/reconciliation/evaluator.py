"""Reconciliation + data-quality evaluation: numerical correctness, missed / false breaks, cause attribution,
cohort recall, root-cause accuracy, duplicate matching, conduct detection."""
from __future__ import annotations

from decimal import Decimal

from db import models as m
from evals.harness import CapabilityResult, Workspace, dataset


def evaluate(ws: Workspace) -> CapabilityResult:
    gold = dataset("data_golden")
    kc, run_id = ws.services.kc, ws.run_id
    breaks = kc.rows(m.ReconciliationBreak, run_id)
    got = {(b.record_key, b.field): b for b in breaks}
    exp = {(e["record_key"], e["field"]): e for e in gold["breaks"]}
    missed = [k for k in exp if k not in got]
    false = [k for k in got if k not in exp]
    numeric = [k for k, e in exp.items() if e.get("delta") is not None]
    correct = [k for k in numeric if k in got and got[k].delta is not None and Decimal(got[k].delta) == Decimal(exp[k]["delta"])]
    clusters = {c.signature: c for c in kc.rows(m.BreakCluster, run_id)}
    cause_ok = sum(1 for sig, names in gold["cluster_causes"].items()
                   if sig in clusters and all(n in (clusters[sig].body.get("linked_rule") or "") for n in names)
                   and "reproduces the source value" in clusters[sig].body.get("hypothesis", ""))
    cohorts = {(c.entity, c.column, c.defect_code): c for c in kc.rows(m.Cohort, run_id)}
    exp_coh = gold["cohorts"]
    coh_hit = [c for c in exp_coh if (c["entity"], c["column"], c["defect_code"]) in cohorts]
    rc_ok = [c for c in coh_hit if cohorts[(c["entity"], c["column"], c["defect_code"])].root_cause.get("cause_code") == c["root_cause"]]
    matches = {tuple(sorted((x.left_ref, x.right_ref))) for x in kc.rows(m.EntityMatch, run_id)}
    exp_matches = {tuple(sorted(p)) for p in gold["entity_matches"]}
    cases = {c.record_key: c for c in kc.rows(m.ConductCase, run_id)}
    over = [r for r in gold["conduct_overcharge_records"] if r in cases and cases[r].impact_type == "overcharge"]
    return CapabilityResult("reconciliation", {
        "numerical_correctness": round(len(correct) / len(numeric), 4),
        "missed_breaks": float(len(missed)),
        "false_breaks": float(len(false)),
        "cause_attribution_accuracy": round(cause_ok / len(gold["cluster_causes"]), 4),
        "cohort_recall": round(len(coh_hit) / len(exp_coh), 4),
        "cohort_precision": round(len(coh_hit) / max(1, len(cohorts)), 4),
        "root_cause_accuracy": round(len(rc_ok) / max(1, len(coh_hit)), 4),
        "duplicate_recall": round(len(matches & exp_matches) / len(exp_matches), 4),
        "duplicate_precision": round(len(matches & exp_matches) / max(1, len(matches)), 4),
        "conduct_detection": round(len(over) / len(gold["conduct_overcharge_records"]), 4),
    }, {"missed": [list(k) for k in missed], "false": [list(k) for k in false]})
