"""LLM-assisted agents: the model drafts advisory prose, the grounding guardrail keeps it honest, and the
deterministic output is always there - with no model, with a failing model and with an ungrounded draft."""
import json

import pytest

from agents.data_quality.cleansing_agent import CleansingAgent
from db import models as m
from evals.harness import Workspace
from reasoning.llm.providers import ScriptedProvider
from schemas.dq import Cohort
from schemas.reasoning import HeuristicResult


def _alternating(grounded: dict, invented: dict):
    """Every other call returns a draft quoting a figure that is not in the facts."""
    state = {"n": 0}

    def handler(_request):
        state["n"] += 1
        return json.dumps(grounded if state["n"] % 2 else invented)
    return handler


SCRIPT = {
    "adjudication_brief": _alternating(
        {"summary": "The two systems compute this rule differently.", "source_reading": "The legacy formula.",
         "target_reading": "The target formula.", "impact": "Customers on affected policies.",
         "questions": ["Which formula did the product terms intend?"]},
        {"summary": "The fee differs by 12345.67 per policy.", "questions": []}),
    "user_story": json.dumps({"title": "Apply the certified rule", "narrative": "As the build team, I need the "
                              "certified rule, so that migrated policies behave as signed off.",
                              "acceptance_criteria": ["Given a policy, when the rule applies, then the certified "
                                                      "expression is used"]}),
    "canonicalise_name": json.dumps({"canonical_name": "NONE", "reason": "no candidate fits"}),
}


@pytest.fixture(scope="module")
def assisted() -> Workspace:
    ws = Workspace.create(llm_provider=ScriptedProvider(SCRIPT))
    ws.run_golden_pipeline()
    yield ws
    ws.close()


def test_adjudication_briefs_keep_grounded_drafts_and_drop_invented_figures(assisted):
    diffs = assisted.services.kc.rows(m.Differential, assisted.run_id)
    assert diffs
    drafted = [d.brief for d in diffs if d.brief.get("narrative")]
    plain = [d.brief for d in diffs if not d.brief.get("narrative")]
    assert drafted, "grounded drafts are attached to the brief"
    assert all(b["model_assisted"]["guard"] == "grounded_narrative passed" for b in drafted)
    assert all("12345.67" not in json.dumps(b) for b in drafted + plain), "invented figures never reach the expert"
    if len(diffs) > 1:
        assert plain, "drafts with invented figures are discarded - the deterministic brief stays"
    # the deterministic brief is complete either way
    assert all(b["question"] and b["exposure_note"] and b["options"] for b in drafted + plain)


def test_stories_are_model_written_but_keep_the_computed_criteria(assisted):
    stories = assisted.services.kc.rows(m.Story, assisted.run_id)
    assert stories
    for s in stories:
        body = s.body
        assert body["title"] == "Apply the certified rule" and body["model_assisted"]
        assert any(c.startswith("(computed)") for c in body["acceptance_criteria"]), body["acceptance_criteria"]


def test_llm_calls_are_recorded_per_purpose_and_pipeline_completes(assisted):
    assert assisted.runs.status(assisted.run_id)["status"] == "completed"
    calls = assisted.services.kc.rows(m.LLMCall, assisted.run_id)
    purposes = {c.record["purpose"] for c in calls}
    assert {"adjudication_brief", "user_story"} <= purposes
    assert all(c.record["prompt_version"] for c in calls)


def test_cleansing_uses_the_model_only_inside_the_closed_action_list(ws):
    def handler(request):
        text = " ".join(str(x["content"]) for x in request.messages)
        action = "quarantine" if "odd_defect" in text else "delete_everything"
        return json.dumps({"action": action, "params": {}, "rationale": "Hold the records for the steward."})

    services = ws.services
    services.llm.provider = ScriptedProvider({"cleansing_proposal": handler})

    def cohort(cid: str, defect: str) -> Cohort:
        return Cohort(cohort_id=cid, run_id="R-LLM", entity="POLICY", column="STATUS", defect_code=defect,
                      description=f"{defect} in STATUS", record_count=3, record_refs=["k1", "k2", "k3"],
                      anomaly=HeuristicResult(heuristic="t", candidate="STATUS", score=0.9, score_components={},
                                              threshold=0.5, passed=True, reason="test", algorithm_version="t1"), evidence=[])

    out = CleansingAgent(services, "R-LLM").run(
        cohorts=[cohort("C1", "odd_defect"), cohort("C2", "strange_defect"), cohort("C3", "null_mandatory")],
        root_causes=[])
    by = {p.cohort_id: p for p in out.items["cleansing"]}
    assert by["C1"].action == "quarantine" and by["C1"].model_assisted
    assert by["C2"].action == "manual_review" and by["C2"].model_assisted is None, "out-of-list action is refused"
    assert by["C3"].model_assisted is None, "playbook entries never go to the model"


def test_without_a_model_every_agent_stays_deterministic(golden):
    diffs = golden.services.kc.rows(m.Differential, golden.run_id)
    assert diffs and not any(d.brief.get("narrative") for d in diffs)
    assert not golden.services.kc.rows(m.LLMCall, golden.run_id) or all(
        c.record["status"] != "ok" for c in golden.services.kc.rows(m.LLMCall, golden.run_id))
