import pytest

from blackboard.control_plane import (
    ControlPlaneViolation, self_test, transition, validate_agent_status, validate_human_decision, validate_llm_purpose,
    validate_signal, validate_tool,
)
from blackboard.identity import AGENTS, AgentIdentity
from schemas.common import HumanRole, ItemStatus, Tier
from schemas.signals import Signal, SignalType


def test_self_test_all_refusals_hold():
    results = self_test()
    assert results and all(ok for _, ok in results), [label for label, ok in results if not ok]


def test_catalog_covers_every_agent_in_the_spec():
    expected = {"cartographer_agent", "cobol_agent", "plsql_agent", "php_agent", "spreadsheet_agent", "document_agent",
                "ddl_agent", "jcl_agent", "normaliser_agent", "differential_agent", "adjudication_agent", "story_agent",
                "mapping_agent", "validation_agent", "codegen_agent", "profiler_agent", "anomaly_agent",
                "root_cause_agent", "cleansing_agent", "live_archive_agent", "entity_match_agent", "reconciler_agent",
                "break_cluster_agent", "lineage_agent", "outcome_agent", "case_routing_agent", "orchestrator_agent",
                "ingest_agent", "retrieval_agent", "assistant_agent", "assurance_agent", "control_reconciler_agent"}
    assert set(AGENTS) == expected
    tiers = {"cartographer_agent": Tier.T3, "cobol_agent": Tier.T1, "php_agent": Tier.T2, "reconciler_agent": Tier.T3,
             "outcome_agent": Tier.T1, "assistant_agent": Tier.T2, "orchestrator_agent": Tier.T3}
    assert all(AGENTS[n].tier is t for n, t in tiers.items())


def test_t3_identity_cannot_hold_llm_or_ats():
    with pytest.raises(ValueError):
        AgentIdentity(name="x", tier=Tier.T3, group="g", job="j", allowed_signal_types=frozenset(),
                      llm_purposes=frozenset({"explain_difference"}))


@pytest.mark.parametrize("status", [ItemStatus.CERTIFIED, ItemStatus.REJECTED])
def test_no_agent_may_certify_or_reject(status):
    for name in AGENTS:
        with pytest.raises(ControlPlaneViolation):
            validate_agent_status(name, status)


def test_only_t3_may_write_automatic():
    validate_agent_status("reconciler_agent", ItemStatus.AUTOMATIC)
    with pytest.raises(ControlPlaneViolation):
        validate_agent_status("mapping_agent", ItemStatus.AUTOMATIC)


def test_failure_paths_never_reach_certified():
    for current in ItemStatus:
        with pytest.raises(ControlPlaneViolation):
            transition(current, ItemStatus.CERTIFIED, actor_type="system")
        with pytest.raises(ControlPlaneViolation):
            transition(current, ItemStatus.CERTIFIED, actor_type="agent")
    assert transition(ItemStatus.PENDING_REVIEW, ItemStatus.UNRESOLVED, actor_type="system") is ItemStatus.UNRESOLVED


def test_decision_roles_and_named_humans():
    validate_human_decision("mapping", HumanRole.DATA_STEWARD, "Dana Steward")
    for bad in ("mapping_agent", "system", "orchestrator", "llm"):
        with pytest.raises(ControlPlaneViolation):
            validate_human_decision("mapping", HumanRole.DATA_STEWARD, bad)
    with pytest.raises(ControlPlaneViolation):
        validate_human_decision("conduct_case", HumanRole.RECONCILIATION_ANALYST, "R. Analyst")


def test_tools_signals_and_llm_purposes_are_authorised_per_identity():
    validate_tool("lineage_agent", "git.read_file", mcp=True)
    with pytest.raises(ControlPlaneViolation):
        validate_tool("assurance_agent", "git.read_file", mcp=True)
    with pytest.raises(ControlPlaneViolation):
        validate_llm_purpose("assistant_agent", "certify_rule")
    with pytest.raises(ControlPlaneViolation):
        validate_llm_purpose("profiler_agent", "explain_difference")
    sig = Signal(signal_id="S", run_id="R", agent="php_agent", tier=Tier.T2, signal_type=SignalType.MAPPING_PROPOSALS,
                 message="x")
    with pytest.raises(ControlPlaneViolation):
        validate_signal(sig)
