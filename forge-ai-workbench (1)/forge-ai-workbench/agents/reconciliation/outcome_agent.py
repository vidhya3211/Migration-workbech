"""Outcome agent (T1): flags real customer impact from the migration. Flags only - a conduct officer decides.

Two sources of customer impact:

1. **Value breaks** - charges/premiums loaded *higher* than expected are overcharges; benefits loaded *lower* are
   benefit reductions; in-scope records not loaded (or rejected at load) may cost a customer their cover.
   Firm-side losses are recorded but are not conduct cases. Breaks stream from PostgreSQL ordered by customer
   record, so a customer's impact is aggregated without holding every break in memory.
2. **Protected rights** (``heuristics.conduct.protected_rights``) - a right the source record carries (a
   guaranteed rate, a protected amount, a protected age, an option rider ...) that the target cannot hold: no
   certified mapping carries it, or the target record has no value for it. Each right is a configured condition
   on the source record, so the business vocabulary belongs to the project, not to the code.

Every outcome is checked against the project's **vulnerability** indicators
(``heuristics.conduct.vulnerability``: conditions on the customer's own record, or on a related record such as
a party, reached through a link field). Outcomes go to the ``conduct_outcome`` working table for Case routing.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Iterator

from agents.base import AgentOutput, BaseAgent
from agents.data_quality.extracts import Extract, load_extracts
from blackboard.identity import agent_identity, register_agent
from reasoning.deterministic.naming import name_tokens
from reasoning.deterministic.predicates import describe, fields_of, matches, row_getter
from schemas.common import ReasoningMode, Tier, stable_id
from schemas.mapping import DictionaryEntry, MappingProposal
from schemas.recon import BreakCluster, ConductOutcome
from schemas.signals import SignalType
from schemas.signals import SignalType as S

DEFAULTS = {"charge_tokens": ["PREMIUM", "FEE", "CHARGE", "COST"],
            "benefit_tokens": ["SURRENDER", "BONUS", "BENEFIT", "FUND", "PAYOUT", "SUM"],
            "annualise": {"MONTHLY": 12, "QUARTERLY": 4, "WEEKLY": 52},
            "protected_rights": [], "vulnerability": []}
NOT_LOADED = {"missing_in_target", "rejected_at_load"}


@register_agent
class OutcomeAgent(BaseAgent):
    IDENTITY = agent_identity("outcome_agent", Tier.T1, "reconciliation", "Flags real customer impact",
                              {S.CONDUCT_OUTCOMES})

    def run(self, clusters: list[BreakCluster], excluded_clusters: set[str] | None = None,
            duplicates: dict[str, str] | None = None, certified_mappings: list[MappingProposal] | None = None,
            dictionary: list[DictionaryEntry] | None = None, **_: object) -> AgentOutput:
        cfg = {**DEFAULTS, **(self.services.settings.heuristics.get("conduct") or {})}
        charge, benefit = set(cfg["charge_tokens"]), set(cfg["benefit_tokens"])
        why = {c.cluster_id: c.hypothesis for c in clusters}
        dups = duplicates or {}
        firm_loss = 0
        by_impact: dict[str, int] = defaultdict(int)
        src = load_extracts(self.services, self.run_id, "source", dictionary or []) if dictionary else []
        tgt = load_extracts(self.services, self.run_id, "target", dictionary or []) if dictionary else []
        vulnerable = self._vulnerable(cfg["vulnerability"], src)

        def from_breaks() -> Iterator[ConductOutcome]:
            nonlocal firm_loss
            current, items = None, []
            for b in self.services.kc.iter_breaks(self.run_id, exclude_clusters=excluded_clusters or set()):
                if b.break_type in ("control_mismatch", "cross_target_drift"):
                    continue   # a ledger control total / a feed between two targets - not a migration outcome
                if b.break_type in NOT_LOADED and b.record_key in dups:
                    continue   # a confirmed duplicate that was merged is not a missing customer
                if b.record_key != current:
                    if items:
                        yield self._outcome(current, items, why, cfg, vulnerable)
                    current, items = b.record_key, []
                tokens = name_tokens(b.field)
                if b.break_type in NOT_LOADED:
                    items.append((b, "missing_record", Decimal(0)))
                    continue
                if b.delta is None:
                    continue
                d = Decimal(b.delta)
                if tokens & charge:
                    if d > 0:
                        items.append((b, "overcharge", d))
                    else:
                        firm_loss += 1
                elif tokens & benefit:
                    if d < 0:
                        items.append((b, "benefit_reduction", -d))
                    else:
                        firm_loss += 1
            if items:
                yield self._outcome(current, items, why, cfg, vulnerable)

        rights_summary: dict[str, int] = {}

        def counted():
            for o in from_breaks():
                by_impact[o.impact_type] += 1
                yield o
            for o in self._rights_lost(cfg["protected_rights"], src, tgt, certified_mappings or [], vulnerable,
                                       rights_summary):
                by_impact[o.impact_type] += 1
                yield o
        n = self.services.kc.save_outcomes(self.run_id, counted())
        self.note_mode(ReasoningMode.DETERMINISTIC)
        msg = (f"{n} customer outcome(s) with potential detriment; {firm_loss} firm-side variance(s) not treated as "
               "conduct")
        if rights_summary:
            msg += "; protected rights not carried by the target: " + ", ".join(
                f"{k} x{v}" for k, v in sorted(rights_summary.items()))
        self.signal(SignalType.CONDUCT_OUTCOMES, msg,
                    data={"by_impact": dict(by_impact), "customers": n, "firm_side": firm_loss,
                          "rights_lost": rights_summary, "vulnerable_customers": len(vulnerable)})
        return AgentOutput(items={"outcomes": [{"customers": n, "by_impact": dict(by_impact),
                                                "rights_lost": rights_summary}]})

    # ------------------------------------------------------------------ value breaks
    def _outcome(self, key: str, items: list, why: dict[str, str], cfg: dict,
                 vulnerable: dict[str, list[str]]) -> ConductOutcome:
        kinds = {k for _, k, _ in items}
        impact = "missing_record" if "missing_record" in kinds else sorted(kinds)[0]
        per_period = sum((x for _, _, x in items), Decimal(0))
        factor = next((f for token, f in cfg["annualise"].items()
                       if any(token in name_tokens(b.field) for b, _, _ in items)), 1)
        reasons = vulnerable.get(key, [])
        return ConductOutcome(
            case_id=stable_id("CC", self.run_id, key), run_id=self.run_id, record_key=key,
            break_ids=[b.break_id for b, _, _ in items], impact_type=impact, detriment=format(per_period, "f"),
            annualised_detriment=format(per_period * factor, "f"), evidence=[e for b, _, _ in items for e in b.evidence],
            rationale="; ".join(f"{b.field}: {k.replace('_', ' ')}" + (f" of {x}" if x else "")
                                + (f" ({b.note})" if b.note else "")
                                + (f" - {why[b.cluster_id][:160]}" if b.cluster_id in why else "")
                                for b, k, x in items),
            elements=sorted({b.field for b, _, _ in items}), vulnerable=bool(reasons), vulnerability_reasons=reasons)

    # ------------------------------------------------------------------ protected rights
    def _rights_lost(self, rights: list[dict], src: list[Extract], tgt: list[Extract],
                     mappings: list[MappingProposal], vulnerable: dict[str, list[str]],
                     summary: dict[str, int]) -> Iterator[ConductOutcome]:
        """One streaming pass per source extract; a record holding several lost rights becomes one outcome.
        Memory stays flat however many records carry a right."""
        if not rights:
            return
        carried = {(m.source.entity.upper(), m.source.field.upper()) for m in mappings}
        target_cols = {(x.entity or "").upper(): {h.upper() for h in x.header} for x in tgt}
        for ex in src:
            if ex.entity is None:
                continue
            header = {h.upper() for h in ex.header}
            live = []
            for right in rights:
                needed = fields_of(right["when"])
                if not needed <= header:
                    continue
                if right.get("source_entity") and right["source_entity"].upper() != ex.entity.upper():
                    continue
                basis = self._loss_basis(right, ex, needed, carried, target_cols)
                if basis is not None:     # None: the target carries the right - value differences are breaks
                    col = next((c for c in ex.header if c.upper() in needed), None)
                    live.append(({**right, "basis": basis}, col))
            if not live:
                continue
            for line_no, row in ex.rows():
                get = row_getter(ex.header, row)
                hits = [(r, col) for r, col in live if matches(r["when"], get)]
                if not hits:
                    continue
                key = ex.record_key(row)
                for r, _ in hits:
                    summary[r["name"]] = summary.get(r["name"], 0) + 1
                reasons = vulnerable.get(key, [])
                yield ConductOutcome(
                    case_id=stable_id("CC", self.run_id, ex.entity, key, "rights"), run_id=self.run_id,
                    record_key=key, break_ids=[], impact_type="right_lost", detriment="0", annualised_detriment="0",
                    evidence=[ex.evidence(line_no, row, col) for _, col in hits],
                    rationale="; ".join(f"{r['name']}: {describe(r['when'])} in the source record - {r['basis']}"
                                        for r, _ in hits),
                    elements=[r["name"] for r, _ in hits], vulnerable=bool(reasons), vulnerability_reasons=reasons)

    @staticmethod
    def _loss_basis(right: dict, ex: Extract, needed: set[str], carried: set[tuple[str, str]],
                    target_cols: dict[str, set[str]]) -> str | None:
        tf = right.get("target_field")
        if tf:
            entity, _, field = str(tf).upper().rpartition(".")
            cols = target_cols.get(entity) if entity else set().union(*target_cols.values()) if target_cols else set()
            if not cols or field not in cols:
                return f"the target has no {tf} to hold it"
            return None
        if not any((ex.entity.upper(), f) in carried for f in needed):
            return "no certified mapping carries it into the target"
        return None

    # ------------------------------------------------------------------ vulnerability
    def _vulnerable(self, indicators: list[dict], src: list[Extract]) -> dict[str, list[str]]:
        """``{record key: [reasons]}`` - conditions evaluated on the record itself, or on a related record
        (``via: {entity, link_field}``: the indicator's extract is keyed by ``link_field`` values found in
        ``entity`` - e.g. party flags reached from the policy's party reference)."""
        out: dict[str, list[str]] = defaultdict(list)
        for ind in indicators:
            needed = fields_of(ind["when"])
            reason = ind.get("reason") or describe(ind["when"])
            hit_keys: set[str] = set()
            for ex in src:
                if needed <= {h.upper() for h in ex.header}:
                    for _, row in ex.rows():
                        if matches(ind["when"], row_getter(ex.header, row)):
                            hit_keys.add(ex.record_key(row))
            via = ind.get("via")
            if via:
                parent = next((x for x in src if x.entity and x.entity.upper() == str(via["entity"]).upper()), None)
                col = parent and next((h for h in parent.header if h.upper() == str(via["link_field"]).upper()), None)
                if parent is None or col is None:
                    continue
                for _, row in parent.rows():
                    if (parent.value(row, col) or "").strip() in hit_keys:
                        out[parent.record_key(row)].append(reason)
            else:
                for k in hit_keys:
                    out[k].append(reason)
        return dict(out)
