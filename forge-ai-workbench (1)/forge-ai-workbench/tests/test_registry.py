"""Nothing is hardcoded to the agents: agents and steps self-register, routing and pipelines are data."""
import pytest

from agents.base import AgentOutput, BaseAgent
from blackboard.identity import (
    AGENTS, AgentRegistrationError, agent_class, agent_identity, extractor_for, extractors, register_agent,
)
from graph.pipeline import PipelineError, compile_pipeline, resolve, step_names
from graph.steps import StepRegistrationError, get_step, step
from schemas.common import Tier
from schemas.signals import SignalType as S


@pytest.fixture(autouse=True)
def _isolated_registries():
    """Plug-ins registered by a test are removed afterwards (the registries are process-wide)."""
    import blackboard.identity as ident
    import graph.steps as steps
    ident.load_agents()
    steps.load_steps()
    before_agents, before_steps = set(ident._identities), set(steps._steps)
    yield
    for name in set(ident._identities) - before_agents:
        ident._identities.pop(name, None)
        ident._classes.pop(name, None)
    for name in set(steps._steps) - before_steps:
        steps._steps.pop(name, None)


def test_agents_are_discovered_not_listed():
    assert len(AGENTS) == 32
    assert set(extractors()) == {"cobol_agent", "plsql_agent", "php_agent", "jcl_agent", "ddl_agent",
                                 "spreadsheet_agent", "document_agent"}
    assert agent_class("reconciler_agent").__name__ == "ReconcilerAgent"


def test_a_plugin_extractor_registers_and_receives_routing():
    @register_agent
    class RpgAgent(BaseAgent):
        IDENTITY = agent_identity("rpg_agent", Tier.T1, "discovery", "Parses RPG programs", {S.CANDIDATE_RULES},
                                  tools={"evidence.read"}, proposes={"rule"}, extractor=True, source_kind="code",
                                  dialect="plsql", handles={"rpg_program"})

        def run(self, artefact, **_):
            return AgentOutput(items={"candidates": []})
    assert "rpg_agent" in AGENTS and extractor_for("rpg_program") == "rpg_agent"
    assert agent_class("rpg_agent") is RpgAgent
    # a project may reroute a type to another registered extractor - never to an unknown agent
    assert extractor_for("plsql", {"plsql": "rpg_agent"}) == "rpg_agent"
    with pytest.raises(AgentRegistrationError):
        extractor_for("plsql", {"plsql": "nobody_agent"})


def test_agent_names_are_unique_and_identity_is_mandatory():
    with pytest.raises(AgentRegistrationError):
        @register_agent
        class Clash(BaseAgent):
            IDENTITY = agent_identity("cobol_agent", Tier.T1, "discovery", "dup", {S.CANDIDATE_RULES})
    with pytest.raises(AgentRegistrationError):
        @register_agent
        class NoIdentity(BaseAgent):
            pass
    with pytest.raises(ValueError):   # an extractor must say what it handles
        agent_identity("half_agent", Tier.T2, "discovery", "x", {S.CANDIDATE_RULES}, extractor=True)


def test_a_plugin_step_joins_a_pipeline_without_code_changes():
    @step("custom_audit", agent="assurance_agent", reads=("facts",), writes=("custom_report",))
    def custom_audit(state, runtime):
        return {"custom_report": {"ok": True}}
    assert get_step("custom_audit").writes == ("custom_report",)
    snap = resolve({"definition": {"name": "custom", "phases": [
        {"steps": ["self_test", "ingest", "cartographer", "custom_audit", "finalize"]}]}}, "default")
    assert step_names(snap) == ["self_test", "ingest", "cartographer", "custom_audit", "finalize"]
    compile_pipeline(snap)


def test_pipeline_validation_refuses_unsafe_definitions():
    with pytest.raises(PipelineError):          # required control-plane step removed
        resolve({"disabled_steps": ["self_test"]}, "default")
    with pytest.raises(StepRegistrationError):  # unknown step
        resolve({"definition": {"name": "x", "phases": [{"steps": ["self_test", "ingest", "cartographer",
                                                                    "finalize", "no_such_step"]}]}}, "default")
    with pytest.raises(PipelineError):          # two parallel lanes writing the same channel
        resolve({"definition": {"name": "x", "phases": [
            {"steps": ["self_test", "ingest", "cartographer"]},
            {"parallel": [["mapping"], ["mapping_copy"]]}, {"steps": ["finalize"]}]}}, "default") \
            if _register_copy() else None
    with pytest.raises(PipelineError):          # unknown pipeline name
        resolve({"name": "does-not-exist"}, "default")


def _register_copy() -> bool:
    from graph.steps import StepSpec, register_step
    register_step(StepSpec(name="mapping_copy", fn=lambda s, r: {}, writes=("mappings",)))
    return True
