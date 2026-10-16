"""End-to-end: the real compiled pipeline over the sample estate on PostgreSQL, durable interrupts, human policy."""
import pytest

from blackboard.control_plane import ControlPlaneViolation, InvalidDecision
from db import models as m
from evals.harness import GATE_ROLES
from schemas.common import HumanRole
from schemas.differential import HumanDecision
from services.run_service import RunService


def test_golden_run_completes_with_governance_intact(golden):
    kc, run = golden.services.kc, golden.run_id
    assert kc.get_run(run).status == "completed"
    comp = kc.completeness(run)
    assert all(c["with_provenance"] == c["rows"] and c["with_audit"] == c["rows"] for c in comp.values())
    tel = kc.telemetry_totals(run)
    assert tel["executions"] >= 25 and tel["executions_without_trace"] == 0 and tel["spans"] > 100
    spans = [s for s in kc.rows(m.TraceSpan, run) if s.agent_id != "assistant_agent"]
    assert {s.trace_id for s in spans} == {kc.get_run(run).trace_id}, "one correlation chain per run"
    status = golden.runs.status(run)
    assert status["progress"]["steps_completed"] == status["progress"]["steps_total"] == 35
    assert status["pipeline"]["name"] == "default"


def test_golden_run_finds_the_business_truth(golden):
    kc, run = golden.services.kc, golden.run_id
    diffs = {d.canonical_name: d for d in kc.rows(m.Differential, run)}
    assert diffs["ADMIN_FEE"].difference_type == "value_mismatch" and diffs["ADMIN_FEE"].financial_exposure == "5"
    assert diffs["RISK_FACTOR"].difference_type == "condition_mismatch"
    assert diffs["TERMINAL_BONUS"].difference_type == "missing_in_target"
    clusters = {c.signature: c for c in kc.rows(m.BreakCluster, run)}
    p7 = clusters["lta_policy.monthly_premium:value_mismatch:-1.46"].body
    assert "ADMIN_FEE" in p7["linked_rule"] and "RISK_FACTOR" in p7["linked_rule"]
    assert kc.get_run(run).summary["go_no_go"]["decision"] == "go"
    # data extracts were staged in PostgreSQL and streamed, never loaded whole
    staged = golden.services.staging.tables(run)
    assert {s.path for s in staged} >= {"data/policies.csv", "data/customers.csv", "data/lta_policy.csv"}
    assert kc.count(m.LiveArchiveScore, run) == 12


def test_certified_rules_name_the_human_who_certified_them(golden):
    rules = golden.services.kc.rows(m.Rule, golden.run_id, status="certified")
    assert rules and all(r.certified_by.startswith("golden-dataset:") and r.certified_role == "actuary_sme" for r in rules)
    audits = golden.services.kc.audit_for("rule", rules[0].rule_id)
    assert [a.what for a in audits] == ["propose", "certify"] and audits[-1].actor_type == "human"


def test_interrupt_survives_restart_and_missing_decisions_never_become_approval(ws):
    run_id = ws.start_sample_run()
    assert ws.runs.status(run_id)["current_gate"] == "rule_adjudication"
    # "process restart": a brand-new service + graph over the same PostgreSQL checkpoint store
    restarted = RunService(ws.services)
    tasks = ws.services.kc.list_tasks(run_id, status="pending", gate="rule_adjudication")
    diff_task = next(t for t in tasks if t.item_type == "differential")
    # decide only one item, then close the gate: every other item must become unresolved, not certified
    restarted.decide_task(diff_task.task_id, HumanDecision(item_id=diff_task.item_id, item_type="differential",
                                                           decision="accept_source", decided_by="Ada Actuary",
                                                           role=HumanRole.ACTUARY_SME), auto_resume=False)
    restarted.resume(run_id, closed_by="Ada Actuary", background=False)
    assert restarted.status(run_id)["current_gate"] == "mapping_approval"
    diffs = ws.services.kc.rows(m.Differential, run_id)
    assert {d.status for d in diffs if d.differential_id != diff_task.item_id} == {"unresolved"}
    assert next(d for d in diffs if d.differential_id == diff_task.item_id).status == "certified"
    agreed = next(t for t in tasks if t.item_type == "rule")
    assert all(ws.services.kc.get(m.Rule, rid).status != "certified" for rid in agreed.brief["rule_ids"])
    from graph.checkpointer import close_checkpointer
    close_checkpointer(restarted.checkpointer)


def test_wrong_role_decisions_are_refused(ws):
    run_id = ws.start_sample_run()
    task = ws.services.kc.list_tasks(run_id, status="pending")[0]
    with pytest.raises(ControlPlaneViolation):
        ws.runs.decide_task(task.task_id, HumanDecision(item_id=task.item_id, item_type=task.item_type, decision="approve",
                                                        decided_by="Dana", role=HumanRole.DATA_STEWARD))
    with pytest.raises(ControlPlaneViolation):
        ws.runs.decide_task(task.task_id, HumanDecision(item_id=task.item_id, item_type=task.item_type, decision="approve",
                                                        decided_by="differential_agent", role=GATE_ROLES["rule_adjudication"]))


def test_kill_switch_halts_at_next_node(ws):
    run_id = ws.start_sample_run()
    ws.runs.halt(run_id, "ops")
    ws.runs.resume(run_id, closed_by="Ada Actuary", background=False)
    st = ws.runs.status(run_id)
    assert st["status"] == "halted" and "kill-switch" in st["halted_reason"]


def test_override_certifies_the_human_expression_once(ws):
    run_id = ws.start_sample_run()
    fee = next(d for d in ws.services.kc.rows(m.Differential, run_id) if d.canonical_name == "ADMIN_FEE")
    with pytest.raises(InvalidDecision):
        ws.runs.decide_item("differential", fee.differential_id, HumanDecision(
            item_id=fee.differential_id, item_type="differential", decision="override", decided_by="Ada",
            role=HumanRole.ACTUARY_SME))
    ws.runs.decide_item("differential", fee.differential_id, HumanDecision(
        item_id=fee.differential_id, item_type="differential", decision="override", decided_by="Ada",
        role=HumanRole.ACTUARY_SME, certified_expression="25.00", rationale="contractual fee"), auto_resume=False)
    ws.runs.resume(run_id, closed_by="Ada", background=False)
    certified = [r for r in ws.services.kc.rows(m.Rule, run_id, status="certified") if r.canonical_name == "ADMIN_FEE"]
    assert [(r.certified_expression, r.certified_by) for r in certified] == [("25.00", "Ada")]


def test_project_pipeline_controls_which_agents_run(ws):
    """A project chooses its pipeline: disabled steps never run, and a run keeps its own snapshot."""
    run_id = ws.start_sample_run(pipeline={"name": "default", "disabled_steps": ["story", "entity_match", "codegen"]})
    run = ws.services.kc.get_run(run_id)
    names = [s for lanes in run.pipeline["phases"] for lane in lanes for s in lane]
    assert "story" not in names and "entity_match" not in names and run.steps_total == 32
    # change the project's pipeline while the run waits at a gate: the waiting run keeps its snapshot
    ws.runs.update_project(run.project_id, "admin", pipeline={"name": "discovery_only"})
    assert ws.services.kc.get_run(run_id).pipeline_hash == run.pipeline_hash
    with pytest.raises(Exception):
        ws.runs.update_project(run.project_id, "admin", pipeline={"name": "default", "disabled_steps": ["self_test"]})


def test_discovery_only_run_stands_down_data_steps(ws):
    pid = ws.project("Discovery estate", pipeline={"name": "discovery_only"})
    from services.run_service import sample_bundle
    up = ws.runs.upload_bytes(pid, *sample_bundle("source"), "Legacy A", "source", "op")
    run_id = ws.runs.start_run(pid, [up.upload_id], "op", background=False)
    st = ws.runs.status(run_id)
    assert st["status"] == "awaiting_human" and st["current_gate"] == "rule_adjudication"
    assert st["progress"]["steps_total"] == 16


def test_non_utf8_extracts_are_staged_without_mojibake(ws):
    """A cp1252 extract is re-encoded to UTF-8 at ingest; staging must read the staged copy as UTF-8."""
    import io
    import zipfile
    from db import models as m
    from services.run_service import sample_bundle
    data, name = sample_bundle("source")
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            out.writestr(item, src.read(item))
        out.writestr("data/fees.csv", "FEE_CODE,DESCRIPTION,AMOUNT\nADM,Administration fee £,25.00\n"
                                      "EXT,Exit fee (Müller scheme),50.00\n".encode("cp1252"))
    pid = ws.project("Encoding estate", pipeline={"name": "discovery_only"})
    up = ws.runs.upload_bytes(pid, buf.getvalue(), name, "Legacy A", "source", "op")
    run_id = ws.runs.start_run(pid, [up.upload_id], "op", background=False)
    art = next(a for a in ws.services.kc.rows(m.Artefact, run_id) if a.path.endswith("fees.csv"))
    assert art.encoding == "utf-8"
    rows = [vals for _line, vals in ws.services.staging.iter_rows(art.artefact_id)]
    assert ["ADM", "Administration fee £", "25.00"] in rows
    assert ["EXT", "Exit fee (Müller scheme)", "50.00"] in rows
