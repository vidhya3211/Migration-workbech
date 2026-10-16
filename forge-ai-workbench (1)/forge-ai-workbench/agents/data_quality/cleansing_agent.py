"""Cleansing agent (T2): proposes a fix per cohort. Nothing is changed - a data steward approves or rejects.

Known defect shapes use the deterministic playbook (configurable per project: ``heuristics.cleansing``). For a
defect the playbook does not cover, or whose cause the root-cause agent could not establish, a model (where
permitted) proposes an action from the closed list with a rationale; the grounding guardrail refuses any
action outside the list or any figure not in the cohort facts, and the steward decides either way.
"""
from __future__ import annotations

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from pydantic import BaseModel, Field

from agents.base import AgentOutput, BaseAgent
from schemas.common import Tier, ReasoningMode, stable_id
from schemas.dq import CleansingProposal, Cohort, RootCauseHypothesis
from schemas.signals import SignalType

ACTIONS = frozenset({"default_value", "reformat", "remap_code", "quarantine", "manual_review",
                     "enrich_from_source"})


class CleansingDraft(BaseModel):
    action: str
    params: dict = Field(default_factory=dict)
    rationale: str


PLAYBOOK = {
    "sentinel_value": ("default_value", {"value": None, "flag": "date_unknown"},
                       "replace the legacy sentinel with NULL and flag the record so the target never stores a fake date"),
    "invalid_date": ("reformat", {"formats": ["%Y%m%d", "%d/%m/%Y"]}, "re-parse using the legacy date formats"),
    "invalid_code": ("remap_code", {"requires_mapping": True},
                     "map the retired/undocumented code to a certified code, or quarantine until the steward decides"),
    "null_mandatory": ("manual_review", {}, "mandatory value missing - obtain from source records"),
    "out_of_range": ("manual_review", {}, "value outside the certified range - verify against source documents"),
    "duplicate_key": ("quarantine", {}, "hold duplicate keys out of the load until they are resolved"),
    "default_value_spike": ("manual_review", {"flag": "date_unverified"},
                            "the date is a conversion default: recover the real value from source history or flag "
                            "it as unknown - never migrate it as fact"),
    "mixed_null_conventions": ("reformat", {"normalise_to": "explicit_null_marker"},
                               "normalise every 'no value' convention to one explicit marker before mapping"),
    "orphan_reference": ("quarantine", {}, "hold orphan child records out of the load; reconcile them against the "
                                           "archived parent index first"),
    "era_correlated_nulls": ("enrich_from_source", {}, "trace the missing values from the era's source records or "
                                                       "an external reference before cutover"),
    "flagged_population": ("manual_review", {}, "treat this population under its own plan before cutover"),
}


@register_agent
class CleansingAgent(BaseAgent):
    IDENTITY = agent_identity("cleansing_agent", Tier.T2, "data_quality", 'Proposes a fix per cohort', {S.CLEANSING_PROPOSALS}, llm={'cleansing_proposal'})

    def run(self, cohorts: list[Cohort], root_causes: list[RootCauseHypothesis], **_: object) -> AgentOutput:
        causes = {r.cohort_id: r for r in root_causes}
        playbook = {**PLAYBOOK, **{k: tuple(v) for k, v in
                                   (self.services.settings.heuristics.get("cleansing") or {}).items()}}
        out = []
        for c in cohorts:
            known = c.defect_code in playbook
            action, params, why = playbook.get(c.defect_code, ("manual_review", {}, "no playbook entry"))
            rc = causes.get(c.cohort_id)
            meta = None
            if not known or (rc is not None and rc.cause_code == "RC-UNRESOLVED"):
                facts = (f"defect {c.defect_code} in {c.entity}.{c.column}: {c.description}; records {c.record_count}; "
                         f"observations {c.anomaly.evidence}")
                draft, meta = self.narrate("cleansing_proposal", {
                    "defect": c.defect_code, "column": f"{c.entity}.{c.column}", "facts": facts,
                    "cause": rc.description if rc else "unknown"}, facts, CleansingDraft,
                    text_of=lambda p: p.rationale, allowed=ACTIONS, value_of=lambda p: p.action)
                if draft is not None:
                    action, params, why = draft.action, draft.params, f"{draft.rationale} (model-proposed)"
            out.append(CleansingProposal(
                proposal_id=stable_id("CLN", self.run_id, c.cohort_id), cohort_id=c.cohort_id, action=action,
                params={**params, "records": c.record_count},
                rationale=why + (f" (likely cause {rc.cause_code}, confidence {rc.score:.2f})" if rc else ""),
                model_assisted=meta))
        self.note_mode(ReasoningMode.DETERMINISTIC)
        self.signal(SignalType.CLEANSING_PROPOSALS, f"{len(out)} cleansing proposal(s)",
                    item_refs=[p.proposal_id for p in out])
        return AgentOutput(items={"cleansing": out})
