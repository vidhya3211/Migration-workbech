"""Run the full evaluation suite and make the release decision (§21-22).

    python -m evals.run_all                      # evaluate, gate, regression-check, write reports
    python -m evals.run_all --update-baseline    # accept current metrics as the new baseline
    python -m evals.run_all --record             # also store results in the configured knowledge core

Exit code 0 = release allowed; 1 = blocked (a floor missed, a regression, or incomplete tracing/audit).
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timezone

from config.settings import get_settings
from evals.ats.evaluator import evaluate as ats_eval
from evals.compression.evaluator import evaluate as compression_eval
from evals.extraction.evaluator import evaluate as extraction_eval
from evals.gate import evaluate_gate
from evals.guardrails.evaluator import evaluate as guardrail_eval
from evals.harness import REPORTS, CapabilityResult, Timer, Workspace, fingerprint
from evals.heuristics.evaluator import evaluate as heuristics_eval
from evals.mapping.evaluator import evaluate as mapping_eval
from evals.reasoning.evaluator import evaluate_differentials, evaluate_llm
from evals.reconciliation.evaluator import evaluate as recon_eval
from evals.regression.baseline import compare, write
from evals.retrieval.evaluator import evaluate as retrieval_eval


def governance(ws: Workspace) -> CapabilityResult:
    comp = ws.services.kc.completeness(ws.run_id)
    rows = sum(c["rows"] for c in comp.values())
    tel = ws.services.kc.telemetry_totals(ws.run_id)
    run = ws.services.kc.get_run(ws.run_id)
    return CapabilityResult("governance", {
        "provenance_accuracy": round(sum(c["with_provenance"] for c in comp.values()) / max(1, rows), 4),
        "audit_completeness": round(sum(c["with_audit"] for c in comp.values()) / max(1, rows), 4),
        "trace_completeness": round((tel["executions"] - tel["executions_without_trace"]) / max(1, tel["executions"]), 4),
        "run_completed": float(run.status == "completed"),
        "estimated_cost": float(run.ledger.get("estimated_cost", 0)),
        "llm_calls": float(run.ledger.get("llm_calls", 0)),
    }, {"completeness": comp, "telemetry": tel})


def run(update_baseline: bool = False, record: bool = False) -> int:
    warnings.filterwarnings("ignore")
    ws = Workspace.create()
    results: list[CapabilityResult] = []
    with Timer() as t:
        ws.run_golden_pipeline()
    results.append(governance(ws))
    for fn in (extraction_eval, evaluate_differentials, mapping_eval, recon_eval, heuristics_eval, ats_eval,
               guardrail_eval, compression_eval, evaluate_llm):
        with Timer() as tt:
            res = fn(ws)
        res.duration_s = tt.seconds
        results.append(res)
    retrieval, assistant = retrieval_eval(ws)
    results += [retrieval, assistant]
    metrics = {r.capability: r.metrics for r in results}
    checks = evaluate_gate(metrics, ws.settings.eval_gate)
    regressions = compare(metrics)
    passed = all(c.passed for c in checks) and not regressions
    fp = fingerprint(ws.settings)
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "fingerprint": fp, "pipeline_seconds": t.seconds,
              "release_allowed": passed, "gate": [c.__dict__ for c in checks], "regressions": regressions,
              "capabilities": {r.capability: {"metrics": r.metrics, "details": r.details, "seconds": r.duration_s}
                               for r in results}}
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "latest.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    (REPORTS / "latest.md").write_text(_markdown(report), encoding="utf-8")
    if update_baseline:
        write(metrics)
    if record:
        from services.runtime import build_services
        kc = build_services(get_settings()).kc
        for r in results:
            kc.save_eval_result(f"EVAL-{fp}-{r.capability}", r.capability,
                                all(c.passed for c in checks if c.capability == r.capability), r.metrics, fp)
    print(_markdown(report))
    ws.close()
    return 0 if passed else 1


def _markdown(report: dict) -> str:
    lines = ["# AI Workbench evaluation report", "", f"- created: {report['created_at']}",
             f"- configuration fingerprint: `{report['fingerprint']}`",
             f"- golden pipeline: {report['pipeline_seconds']}s",
             f"- **release {'ALLOWED' if report['release_allowed'] else 'BLOCKED'}**", "", "## Gate", "",
             "| check | metric | value | threshold | result |", "|---|---|---|---|---|"]
    for c in report["gate"]:
        lines.append(f"| {c['key']} | {c['capability']}.{c['metric']} | {c['value']} | {c['threshold']} | "
                     f"{'pass' if c['passed'] else '**FAIL**'} |")
    lines += ["", "## Regressions", ""] + ([f"- {r}" for r in report["regressions"]] or ["- none"])
    lines += ["", "## Capabilities", ""]
    for cap, body in report["capabilities"].items():
        lines.append(f"### {cap}")
        lines += [f"- {k}: {v}" for k, v in body["metrics"].items()]
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--record", action="store_true")
    a = ap.parse_args()
    sys.exit(run(a.update_baseline, a.record))

