"""Production assurance logic carried over from the proof of concept as configurable policy: reconciliation
tolerances, migration scope, the record waterfall, systematic-shift clustering, conduct rights and
vulnerability, population data-quality patterns and record linkage."""
import io
from decimal import Decimal

import pytest
from sqlalchemy import text

from agents.data_quality.extracts import Extract
from agents.data_quality.patterns import PatternDetector
from agents.data_quality.anomaly_agent import _is_date_column
from db import models as m
from evals.harness import Workspace
from reasoning.deterministic.predicates import matches
from reasoning.deterministic.tolerance import Tolerance, tolerance_for
from reasoning.heuristics.duplicate.scoring import score as dup_score, transposed
from schemas.artefact import Artefact
from schemas.common import SystemRef, SystemRole, new_id


# ------------------------------------------------------------------ pure policy
def test_tolerance_is_the_larger_of_floor_and_basis_points():
    cfg = {"tolerances": {"by_type": {"decimal": {"absolute_floor": "0.01", "relative_bp": 1}},
                          "fields": {"POLICY.UNITS": {"absolute_floor": "0.0001"}, "SUM_ASSURED": {}}}}
    t = tolerance_for(cfg, "POLICY", "FUND_VALUE", "decimal")
    assert t.within(Decimal("100.00"), Decimal("0.01")) and not t.within(Decimal("100.00"), Decimal("0.02"))
    assert t.within(Decimal("1000000"), Decimal("99")) and not t.within(Decimal("1000000"), Decimal("101"))   # 1bp
    assert tolerance_for(cfg, "POLICY", "UNITS", "decimal").absolute_floor == Decimal("0.0001")
    assert tolerance_for(cfg, "POLICY", "NAME", "string").exact and Tolerance().exact


def test_condition_language():
    rec = {"STATUS": "l", "AGE": "61", "LAST_PREM": "2018-05-01", "FLAG": ""}.get
    assert matches({"field": "STATUS", "in": ["L", "X"]}, rec)
    assert matches({"all": [{"field": "AGE", "gte": 60}, {"field": "LAST_PREM", "before": "2019-01-01"}]}, rec)
    assert matches({"any": [{"field": "FLAG", "present": True}, {"field": "AGE", "lt": 65}]}, rec)
    assert not matches({"field": "MISSING", "equals": "Y"}, rec)


def test_record_linkage_signals():
    a = {"forename": "Bill", "surname": "Smith", "dob": "19800312", "postcode": "LS1 1AA", "national_id": "QQ1"}
    b = {"forename": "William", "surname": "Smith", "dob": "19801203", "postcode": "LS1 1AA", "national_id": ""}
    assert transposed("19800312", "19801203")
    plain = dup_score("a", "b", a, b, 0.85)
    nick = dup_score("a", "b", a, b, 0.85, nicknames={"BILL": "WILLIAM"})
    assert nick.score > plain.score and nick.score_components["dob"] == 0.8
    strong = dup_score("a", "c", a, {**b, "national_id": "QQ1", "forename": "W"}, 0.85)
    assert strong.score >= 0.98 and strong.reason.endswith("strong")
    conflict = dup_score("a", "d", a, {**a, "national_id": "QQ2"}, 0.85)
    assert "strong identifier CONFLICTS" in conflict.evidence and conflict.score < 1.0


# ------------------------------------------------------------------ SQL-level behaviour
def _run(ws: Workspace) -> tuple[str, str]:
    pid = ws.project(f"Logic {new_id('X')[-6:]}")
    run_id = new_id("RUN")
    ws.services.kc.create_run(run_id, "test", "a", "b", project_id=pid, pipeline={"name": "default"},
                              pipeline_hash="t", steps_total=1)
    return pid, run_id


def _breaks(ws, run_id, rows):
    with ws.services.engine.begin() as c:
        for i, (field, expected, delta) in enumerate(rows):
            c.execute(text("INSERT INTO reconciliation_break (break_id, run_id, record_key, entity, field, break_type, "
                           "source_value, expected_value, target_value, delta, status, lineage, body, created_at) "
                           "VALUES (:id, :r, :k, 'POLICY', :f, 'value_mismatch', :e, :e, :t, :d, 'automatic', "
                           "'{}'::jsonb, '{}'::jsonb, now())"),
                      {"id": f"BRK-{field}-{i}", "r": run_id, "k": f"K{i}", "f": field, "e": str(expected),
                       "t": str(Decimal(expected) + Decimal(delta)), "d": str(delta)})


def test_systematic_shift_and_band_clustering(ws):
    _, run_id = _run(ws)
    shift = [("PREMIUM", Decimal(100 + i * 7), (Decimal(100 + i * 7) * Decimal("0.02")).quantize(Decimal("0.01")))
             for i in range(30)]
    exact = [("FEE", Decimal("25.00"), Decimal("5.00"))] * 3
    noisy = [("VALUE", Decimal(1000 + i), Decimal(i * i % 97 + 1) * (1 if i % 2 else -1)) for i in range(60)]
    _breaks(ws, run_id, shift + exact + noisy)
    groups = ws.services.kc.break_groups(run_id, {"min_cohort": 25, "max_dispersion": 0.35,
                                                  "max_signatures_per_field": 50, "material_band": 0.02})
    by_field = {}
    for g in groups:
        by_field.setdefault(g["field"], []).append(g)
    assert [g["kind"] for g in by_field["PREMIUM"]] == ["shift"] and by_field["PREMIUM"][0]["count"] == 30
    assert abs(by_field["PREMIUM"][0]["shape"]["median_shift"] - 0.02) < 0.001
    assert [g["kind"] for g in by_field["FEE"]] == ["exact"] and by_field["FEE"][0]["count"] == 3
    assert {g["kind"] for g in by_field["VALUE"]} == {"band"} and sum(g["count"] for g in by_field["VALUE"]) == 60
    # membership filters select exactly the grouped breaks
    for g in groups:
        assert len(ws.services.kc.break_ids(run_id, g, limit=1000)) == g["count"]


def _extract(ws, pid, run_id, name, csv_text, key) -> Extract:
    aid = new_id("ART")
    st = ws.services.staging.stage_delimited(artefact_id=aid, project_id=pid, run_id=run_id, system_name="legacy",
                                             system_role="source", path=f"data/{name}.csv",
                                             stream=io.BytesIO(csv_text.encode()), encoding="utf-8", delimiters=[","])
    art = Artefact(artefact_id=aid, run_id=run_id, upload_id="U", system=SystemRef(system_name="legacy",
                   system_role=SystemRole.SOURCE), path=f"data/{name}.csv", artefact_type="data_extract",
                   sha256="0" * 64, size_bytes=len(csv_text), confidence=1.0, blob_uri="obj://x/y")
    return Extract(art, st, ws.services.staging, name.upper(), {}, key)


def test_population_patterns(ws):
    pid, run_id = _run(ws)
    lines = ["MEMBER_ID,JOIN_DT,NINO,MVR_FREE_DT,STATUS"]
    for i in range(400):
        join = "1990-01-01" if i < 60 else f"{1975 + i % 45}-{1 + i % 12:02d}-{1 + i % 27:02d}"
        year = int(join[:4])
        nino = "" if year < 2002 and i % 3 else f"QQ{i:06d}"
        mvr = "" if i % 50 == 0 else "99999999" if i % 51 == 0 else f"20{30 + i % 9}-01-01"
        lines.append(f"{1000 + i},{join},{nino},{mvr},{'C' if i % 40 == 0 else 'A'}")
    member = _extract(ws, pid, run_id, "member", "\n".join(lines) + "\n", "MEMBER_ID")
    contrib = _extract(ws, pid, run_id, "contrib", "CONTRIB_ID,MEMBER_ID,AMOUNT\n" + "".join(
        f"{i},{1000 + i if i % 25 else 99000 + i},10.00\n" for i in range(200)), "CONTRIB_ID")
    det = PatternDetector(run_id, {"relationships": [{"child": "CONTRIB.MEMBER_ID", "parent": "MEMBER.MEMBER_ID"}],
                                   "flag_cohorts": [{"name": "claims in flight", "when": {"field": "STATUS", "equals": "C"}}]},
                          ["00000000", "99999999"], 20, 5)
    found = {(c.column, c.defect_code): c for c in det.detect([member, contrib], _is_date_column)}
    spike = found[("JOIN_DT", "default_value_spike")]
    assert spike.record_count == 60 and any("contiguous block" in e for e in spike.anomaly.evidence)
    assert found[("NINO", "era_correlated_nulls")].record_count > 0
    assert found[("MVR_FREE_DT", "mixed_null_conventions")].record_count > 0
    assert found[("MEMBER_ID", "orphan_reference")].record_count == 8
    assert found[("STATUS", "flagged_population")].record_count == 10


# ------------------------------------------------------------------ the pipeline, with project policy
POLICY = {"heuristics": {
    "reconciliation": {"tolerances": {"fields": {"MONTHLY_PREMIUM": {"absolute_floor": "5.00"}}},
                       "scope": {"POLICY": [{"field": "POL_NUMBER", "equals": "P000000012",
                                             "reason": "closed scheme - not migrated"}]}},
    "conduct": {"protected_rights": [{"name": "smoker_terms", "when": {"field": "SMOKER_FLAG", "equals": "Y"},
                                      "target_field": "LTA_POLICY.SMOKER_GUARANTEE", "materiality": "high",
                                      "route": "product-actuary"}],
                "vulnerability": [{"when": {"field": "POSTCODE", "starts_with": "M1"}, "reason": "support needs",
                                   "via": {"entity": "POLICY", "link_field": "CUST_ID"}}]},
    "data_quality": {"flag_cohorts": [{"name": "lapsed policies", "when": {"field": "POL_STATUS", "equals": "L"}}]},
    "live_archive": {"overrides": [{"when": {"field": "POL_STATUS", "equals": "L"}, "floor": 0.9,
                                    "reason": "test override"}]},
}}


@pytest.fixture(scope="module")
def policy_run() -> Workspace:
    ws = Workspace.create()
    ws.project("Policy estate", settings_overrides=POLICY)
    ws.run_golden_pipeline()
    yield ws
    ws.close()


def test_scope_and_tolerance_shape_the_waterfall(policy_run):
    ws = policy_run
    run = ws.services.kc.get_run(ws.run_id)
    wf = {w["source_entity"]: w for w in run.summary["waterfall"]}
    pol = wf["POLICY"]
    assert pol["balanced"] and pol["excluded"] == 1 and pol["missing"] == 0
    assert pol["excluded_reasons"] == {"closed scheme - not migrated": 1}
    breaks = ws.services.kc.rows(m.ReconciliationBreak, ws.run_id)
    assert not [b for b in breaks if b.field == "monthly_premium"], "differences inside the tolerance are not breaks"
    assert not [b for b in breaks if b.record_key == "P000000012"], "out-of-scope records are not missing customers"


def test_conduct_rights_vulnerability_and_routing(policy_run):
    ws = policy_run
    cases = {c.record_key: c for c in ws.services.kc.rows(m.ConductCase, ws.run_id)}
    rights = [c for c in cases.values() if c.impact_type == "right_lost"]
    assert rights and all(c.band == "high" and c.route == "product-actuary" for c in rights)
    assert all(c.body["duty_outcome"] == "products-services" for c in rights)
    assert cases["P000000002"].body["outcome"]["vulnerable"] is True
    assert "P000000012" not in cases


def test_flags_and_threshold_curve(policy_run):
    ws = policy_run
    cohorts = ws.services.kc.rows(m.Cohort, ws.run_id)
    assert any(c.defect_code == "flagged_population" and c.record_count == 1 for c in cohorts)
    la = ws.services.kc.get_run(ws.run_id).summary["live_archive"]
    assert la["overridden"] == 1 and len(la["threshold_curve"]) == 9
