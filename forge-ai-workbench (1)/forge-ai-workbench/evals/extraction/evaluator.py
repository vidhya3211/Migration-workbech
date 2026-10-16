"""Extraction evaluation: precision, recall, evidence accuracy, locator accuracy, unsupported claims."""
from __future__ import annotations

from db import models as m
from evals.harness import CapabilityResult, Workspace, dataset


def _line(p) -> str:
    loc = p.locator
    return str(loc.get("line_start") or loc.get("cell_range") or "")


def evaluate(ws: Workspace) -> CapabilityResult:
    gold = dataset("extraction_golden")
    kc, run_id = ws.services.kc, ws.run_id
    extracted = []
    for r in kc.rows(m.Rule, run_id):
        prov = kc.provenance_for("rule", r.rule_id)
        if not prov:
            extracted.append((None, r))
            continue
        extracted.append((prov[0], r))
    keys = {(p.locator.get("file"), r.kind, r.canonical_name, _line(p)) for p, r in extracted if p is not None}
    expected = {(e["file"], e["kind"], e["canonical_name"], e["line"]) for e in gold["expected"]}
    found = expected & keys
    in_scope = {k for k in keys if k[0] in set(gold["scope"])}
    exp_scope = {e for e in expected if e[0] in set(gold["scope"])}
    unsupported = sum(1 for p, _ in extracted if p is None)
    verifier = ws.services.verifier(run_id)
    from schemas.provenance import Evidence
    exact = 0
    checked = 0
    for p, r in extracted:
        if p is None:
            continue
        checked += 1
        ev = Evidence.model_validate({"artefact_id": p.artefact_id, "locator": p.locator, "excerpt": p.excerpt,
                                      "source_sha256": p.source_sha256,
                                      "system": {"system_name": p.system_name, "system_role": p.system_role}})
        exact += verifier.verify(ev) is None
    run = kc.get_run(run_id)
    facts = (run.summary or {}) if run else {}
    return CapabilityResult("extraction", {
        "precision": round(len(in_scope & exp_scope) / len(in_scope), 4) if in_scope else 0.0,
        "recall": round(len(found) / len(expected), 4),
        "locator_accuracy": round(exact / checked, 4) if checked else 0.0,
        "evidence_accuracy": round(exact / checked, 4) if checked else 0.0,
        "unsupported_claim_rate": round(unsupported / max(1, len(extracted)), 4),
        "rules_extracted": float(len(extracted)),
    }, {"missed": sorted(map(list, expected - keys))[:30], "extra_in_scope": sorted(map(list, in_scope - exp_scope))[:30],
        "artefacts": facts.get("artefacts")})
