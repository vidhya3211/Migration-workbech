from reasoning.ats.budget import ATSBudget
from reasoning.ats.engine import ATSEngine
from reasoning.ats.node import ATSNode, Candidate
from reasoning.heuristics.mapping.heuristics import score_pair
from reasoning.heuristics.naming_match import score as name_match
from reasoning.router.router import ReasoningTask, route
from schemas.common import ReasoningMode, SystemRef, SystemRole
from schemas.mapping import DictionaryEntry
from schemas.provenance import Evidence, SchemaLocator


class Explosive:
    """A policy that would branch forever - ATS must stop it."""
    name = "explosive"

    def root(self):
        return Candidate(hypothesis="root", state={"n": 0})

    def expand(self, node: ATSNode):
        return [Candidate(hypothesis=f"h{node.state['n']}-{i}", state={"n": node.state["n"] * 10 + i + 1}) for i in range(5)]

    def score(self, node):
        return 0.5, 0.0

    def violates(self, node):
        return None

    def is_terminal(self, node):
        return False

    def state_key(self, node):
        return node.state["n"]


def test_ats_is_bounded_and_never_selects_without_confidence():
    res = ATSEngine(ATSBudget(max_branches=40, max_depth=3, max_children=3)).search(Explosive())
    assert res.best is None
    assert res.branches <= 41 and res.max_depth_reached <= 3
    assert res.termination_reason


def test_router_uses_minimum_mechanisms():
    assert route(ReasoningTask(task="t", deterministic_sufficient=True)).route == [ReasoningMode.DETERMINISTIC]
    plan = route(ReasoningTask(task="t", candidates=4, close_alternatives=2, needs_semantics=True, llm_available=False))
    assert plan.route == [ReasoningMode.DETERMINISTIC, ReasoningMode.HEURISTIC, ReasoningMode.ATS]
    assert plan.requires_human_interpretation


def _entry(field, dtype="string", length=10, role=SystemRole.SOURCE):
    ref = SystemRef(system_name="S" if role is SystemRole.SOURCE else "T", system_role=role)
    return DictionaryEntry(entry_id=field, system=ref, entity="E", field=field, data_type=dtype, length=length,
                           evidence=Evidence(artefact_id="A", source_sha256="0" * 64, system=ref, excerpt="x",
                                             locator=SchemaLocator(file="f", object_name="E", line_start=1, line_end=1)))


def test_heuristic_results_are_explainable():
    h = score_pair(_entry("POL_NUMBER"), _entry("policy_ref", role=SystemRole.TARGET), {"name": .4, "type": .2,
                                                                                      "length": .1, "domain": .15}, 0.55)
    assert h.passed and h.algorithm_version and h.score_components["name"] > 0.8 and h.evidence and h.reason
    weak = score_pair(_entry("LAST_PREM_DT", length=8), _entry("policy_ref", role=SystemRole.TARGET),
                      {"name": .4, "type": .2, "length": .1, "domain": .15}, 0.55)
    assert not weak.passed   # type/length compatibility alone is not a mapping
    assert name_match("monthly administration charge", "ADMIN_FEE", threshold=0.7).passed
    assert not name_match("Base rate", "BASE_PREMIUM", threshold=0.7).passed
