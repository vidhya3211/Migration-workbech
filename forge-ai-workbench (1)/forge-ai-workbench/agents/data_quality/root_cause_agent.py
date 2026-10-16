"""Root-cause agent (T1): matches a cohort to a known cause. Recommendation only - a steward decides.

ATS explores the known-cause catalogue, testing each hypothesis against the cohort's deterministic facts;
the LLM (if permitted and available) may add hypotheses, which ATS then has to support with evidence.
"""
from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel, Field

from blackboard.identity import agent_identity, register_agent
from schemas.common import Tier
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from agents.data_quality.anomaly_agent import OBSERVED
from context.headroom.policy import ContentClass
from reasoning.ats.policies import RootCausePolicy
from reasoning.llm.gateway import Evidence as LLMEvidence
from reasoning.router.router import ReasoningTask
from schemas.dq import Cohort, RootCauseHypothesis
from schemas.signals import SignalType


class LLMHypotheses(BaseModel):
    hypotheses: list[dict] = Field(default_factory=list)


@register_agent
class RootCauseAgent(BaseAgent):
    IDENTITY = agent_identity("root_cause_agent", Tier.T1, "data_quality", 'Matches a cohort to a known cause', {S.ROOT_CAUSES}, llm={'root_cause_hypotheses'}, ats=True)

    def run(self, cohorts: list[Cohort], **_: object) -> AgentOutput:
        catalogue = [c.model_dump() for c in self.services.settings.root_causes]
        out: list[RootCauseHypothesis] = []
        for c in cohorts:
            facts = self._facts(c)
            self.plan(ReasoningTask(task=f"root cause {c.defect_code}", deterministic_possible=True,
                                    candidates=len(catalogue), close_alternatives=len(catalogue)))
            result = self.ats(RootCausePolicy(c.defect_code, facts, catalogue))
            best = result.best
            extra = []
            if best is None:   # no catalogue cause is evidenced: semantic interpretation is now justified
                plan = self.plan(ReasoningTask(task=f"root cause {c.defect_code} (uncatalogued)",
                                               deterministic_possible=False, needs_semantics=True))
                if not plan.requires_human_interpretation:
                    res = self.llm("root_cause_hypotheses",
                                   {"facts": str(facts), "catalogue": str([x["code"] for x in catalogue])},
                                   [LLMEvidence("facts", str(facts), ContentClass.STRUCTURED_BULK)], LLMHypotheses)
                    if res.parsed:
                        extra = [h.get("code") for h in res.parsed.hypotheses if h.get("code")]
            out.append(RootCauseHypothesis(
                cohort_id=c.cohort_id, cause_code=best.hypothesis if best else "RC-UNRESOLVED",
                description=next((x["description"] for x in catalogue if best and x["code"] == best.hypothesis),
                                 "no catalogue cause reached the confidence threshold - human investigation"),
                score=best.confidence if best else 0.0, evidence=best.evidence if best else [],
                alternatives=[a.hypothesis for a in result.alternatives] + [f"llm:{e}" for e in extra],
                ats_run_id=result.ats_run_id))
        self.signal(SignalType.ROOT_CAUSES, f"root-cause recommendations for {len(out)} cohort(s)",
                    item_refs=[h.cohort_id for h in out],
                    data={"by_cause": {h.cause_code: sum(1 for x in out if x.cause_code == h.cause_code) for h in out}})
        return AgentOutput(items={"root_causes": out})

    def _facts(self, c: Cohort) -> dict:
        observed: list[str] = []
        for e in c.anomaly.evidence:
            if e.startswith(OBSERVED):
                try:
                    observed = [str(x) for x in json.loads(e[len(OBSERVED):])]
                except ValueError:
                    observed = []
        facts: dict = {"defect": c.defect_code, "records": c.record_count}
        if c.defect_code == "sentinel_value":
            facts["sentinel_values"] = observed
        if c.defect_code == "invalid_code":
            facts["unknown_codes"] = observed
        if c.defect_code == "out_of_range":
            facts["range_violation"] = observed
        if c.defect_code == "invalid_date":
            # the standard parser already failed: does the value read in a layout the platform does not expect?
            formats = (self.services.settings.heuristics.get("root_cause") or {}).get("alternative_date_formats",
                                                                                         ALT_DATE_FORMATS)
            facts["observed_dates"] = observed
            facts["parseable_other_format"] = any(_parses(v, formats) for v in observed)
        return facts


ALT_DATE_FORMATS = ("%m/%d/%Y", "%d.%m.%Y", "%d%m%Y", "%y%m%d", "%d-%b-%Y", "%d %b %Y", "%d/%m/%y", "%m-%d-%Y")


def _parses(value: str, formats) -> bool:
    for fmt in formats:
        try:
            datetime.strptime(value.strip(), fmt)
            return True
        except ValueError:
            continue
    return False
