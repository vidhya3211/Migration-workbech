"""Case-routing agent (T1): routes conduct cases by materiality to the right queue and priority.

Routing is a recommendation; the conduct officer decides remediation. Raising the Azure DevOps case goes
through the governed MCP tool and only with a recorded human approval.

For the highest-priority cases (``heuristics.conduct.impact_notes_max`` per run) a model, where permitted,
drafts the customer-impact note and the remediation options for the officer - quoting only the case's own
figures (grounding guardrail). The officer authors the decision.

Project policy (``heuristics.conduct``): a vulnerable customer lifts a case one band (``vulnerability_uplift``);
a lost protected right takes at least the band configured for it; each case names the Consumer Duty outcome it
bears on (``duty_outcomes`` by impact type or right); ``routing`` rules send matching cases to a specialist
queue (for example a product actuary) instead of the general conduct queue.
"""
from __future__ import annotations

from decimal import Decimal

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from pydantic import BaseModel, Field

from agents.base import AgentOutput, BaseAgent
from reasoning.heuristics.conduct.materiality import score as materiality
from schemas.common import Tier, ReasoningMode
from typing import Callable

from schemas.recon import ConductCase
from schemas.signals import SignalType

PRIORITY = {"high": 1, "medium": 2, "low": 3}
ORDER = ["low", "medium", "high"]
DUTY_DEFAULTS = {"overcharge": "price-value", "benefit_reduction": "products-services",
                 "missing_record": "consumer-support", "right_lost": "products-services",
                 "underpayment": "price-value"}


def _max_band(a: str, b: str | None) -> str:
    if b not in ORDER:
        return a
    return a if ORDER.index(a) >= ORDER.index(b) else b


class ImpactNote(BaseModel):
    impact: str
    options: list[str] = Field(default_factory=list)
    question_for_the_human: str = ""


@register_agent
class CaseRoutingAgent(BaseAgent):
    IDENTITY = agent_identity("case_routing_agent", Tier.T1, "reconciliation", "Routes conduct cases by materiality",
                              {S.CASE_ROUTES, S.HUMAN_REVIEW_REQUIRED}, mcp={"devops.create_work_item"},
                              proposes={"conduct_case"}, llm={'conduct_impact_note'})

    def run(self, sink: Callable[[list[ConductCase]], None] | None = None, **_: object) -> AgentOutput:
        bands = self.services.settings.heuristics.get("conduct", {}).get("materiality_bands") or [
            {"band": "low", "min": 0}, {"band": "medium", "min": 25}, {"band": "high", "min": 250}]
        batch, collected = [], []
        by_band = {b: 0 for b in PRIORITY}
        size = self.services.settings.scale.bulk_batch_size
        conduct = self.services.settings.heuristics.get("conduct") or {}
        notes_left = int(conduct.get("impact_notes_max", 25))
        rights = {r["name"]: r for r in conduct.get("protected_rights") or []}
        duty = {**DUTY_DEFAULTS, **(conduct.get("duty_outcomes") or {})}
        uplift = conduct.get("vulnerability_uplift", {"from": "medium", "to": "high"})
        routes = conduct.get("routing") or []

        def flush():
            if batch:
                (sink or collected.extend)(list(batch))
                batch.clear()
        for o in self.services.kc.iter_outcomes(self.run_id):
            h, band = materiality(o.case_id, o.impact_type, Decimal(o.annualised_detriment), o.vulnerable, bands)
            if o.impact_type == "missing_record":
                band = "high"      # unquantifiable: the customer may have lost cover
            for el in o.elements:
                band = _max_band(band, (rights.get(el) or {}).get("materiality"))
            if o.vulnerable and uplift and band == uplift.get("from"):
                band = uplift.get("to", band)
            outcome = next((duty[e] for e in o.elements if e in duty),
                           next((rights[e].get("duty_outcome") for e in o.elements
                                 if e in rights and rights[e].get("duty_outcome")), duty.get(o.impact_type)))
            route = next((r["route"] for r in routes
                          if (not r.get("elements_any") or set(r["elements_any"]) & set(o.elements))
                          and (not r.get("impact_types") or o.impact_type in r["impact_types"])),
                         next((rights[e]["route"] for e in o.elements if e in rights and rights[e].get("route")),
                              f"conduct-{band}"))
            by_band[band] = by_band.get(band, 0) + 1
            note = None
            if band == "high" and notes_left > 0:
                facts = (f"impact {o.impact_type}; detriment {o.detriment}; annualised {o.annualised_detriment}; "
                         f"band {band}; vulnerable customer: {'yes - ' + ', '.join(o.vulnerability_reasons) if o.vulnerable else 'no'}; "
                         f"Consumer Duty outcome: {outcome}; findings: {o.rationale}")
                draft, meta = self.narrate("conduct_impact_note", {
                    "impact_type": o.impact_type, "detriment": o.detriment, "annualised": o.annualised_detriment,
                    "band": band, "rationale": (f"vulnerable customer ({', '.join(o.vulnerability_reasons)}); "
                                                if o.vulnerable else "") + o.rationale[:1400]}, facts, ImpactNote,
                    text_of=lambda p: " ".join([p.impact, *p.options, p.question_for_the_human]))
                if draft is not None:
                    note = {**draft.model_dump(), **(meta or {})}
                    notes_left -= 1
            batch.append(ConductCase(case_id=o.case_id, run_id=self.run_id, outcome=o, materiality=h, band=band,
                                     route=route, priority=PRIORITY.get(band, 3), impact_note=note,
                                     duty_outcome=outcome))
            if len(batch) >= size:
                flush()
        flush()
        self.note_mode(ReasoningMode.HEURISTIC)
        self.signal(SignalType.CASE_ROUTES, f"{sum(by_band.values())} conduct case(s) routed for the conduct officer",
                    item_refs=[c.case_id for c in collected][:500], data={"by_band": by_band})
        return AgentOutput(items={"cases": collected, "summary": [by_band]})
