"""Assurance agent (T3): scores every capability's output quality, every run (UC16, §45).

Covers extraction, provenance, audit, tracing, reasoning (differentials), heuristics/mapping, ATS, data
quality, reconciliation, conduct, assistant, guardrails, Headroom compression, cost and latency - against the
configured floors. Mechanical: it measures; it does not decide go/no-go.
"""
from __future__ import annotations

from decimal import Decimal

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from agents.discovery.normaliser_agent import source_kind
from db import models as m
from guardrails.pipeline import GuardrailContext, GuardrailViolation, Stage
from guardrails.provenance.policies import AuditSubject
from reasoning.deterministic.readiness import compute as readiness
from schemas.build import AssuranceReport, CapabilityScore
from schemas.common import Tier, ItemStatus, ReasoningMode, new_id
from schemas.signals import SignalLevel, SignalType

PROTECTED = {"system_prompt", "source_code_evidence", "source_locator", "regulatory_evidence", "financial_input",
             "reconciliation_value", "conduct_evidence", "guard_input", "provenance", "audit", "question"}


def _ratio(a: int, b: int) -> float:
    return 1.0 if b == 0 else round(a / b, 4)


@register_agent
class AssuranceAgent(BaseAgent):
    IDENTITY = agent_identity("assurance_agent", Tier.T3, "cross_cutting", "Scores every capability's output quality, every run", {S.ASSURANCE_REPORT})

    def run(self, run_facts: dict, **_: object) -> AgentOutput:
        kc, gate = self.services.kc, self.services.settings.eval_gate
        caps: list[CapabilityScore] = []

        def cap(name: str, metrics: dict[str, float], thresholds: dict[str, float], notes: list[str] | None = None,
                maxima: set[str] | None = None) -> None:
            ok = all((metrics[k] <= v) if k in (maxima or set()) else (metrics[k] >= v)
                     for k, v in thresholds.items() if k in metrics)
            caps.append(CapabilityScore(capability=name, metrics=metrics, thresholds=thresholds, passed=ok,
                                        notes=notes or []))

        f = run_facts
        cap("extraction", {"guard_acceptance": _ratio(f.get("rules_accepted", 0), f.get("rules_candidates", 0)),
                           "artefacts_analysed": _ratio(f.get("artefacts_analysed", 0), f.get("artefacts_routable", 0))},
            {"guard_acceptance": 0.9, "artefacts_analysed": 1.0},
            [f"{f.get('rules_refused', 0)} candidate(s) refused by the deterministic guard"])
        comp = kc.completeness(self.run_id)
        rows = sum(c["rows"] for c in comp.values())
        prov = _ratio(sum(c["with_provenance"] for c in comp.values()), rows)
        aud = _ratio(sum(c["with_audit"] for c in comp.values()), rows)
        try:
            self.services.guardrails.run(Stage.AUDIT, AuditSubject(comp), GuardrailContext(self.run_id, self.name))
        except GuardrailViolation:
            pass   # recorded as a guardrail event; the capability below fails explicitly
        cap("provenance", {"provenance_accuracy": prov}, {"provenance_accuracy": gate["provenance_accuracy"]})
        cap("audit", {"audit_completeness": aud}, {"audit_completeness": gate["audit_completeness"]})
        tel = kc.telemetry_totals(self.run_id)
        cap("tracing", {"trace_completeness": _ratio(tel["executions"] - tel["executions_without_trace"], tel["executions"]),
                        "spans": float(tel["spans"])}, {"trace_completeness": gate["trace_completeness"]})
        diffs = kc.rows(m.Differential, self.run_id)
        proven = sum(1 for d in diffs if d.body.get("divergence") or d.difference_type.startswith("missing")
                     or d.difference_type == "document_conflict")
        cap("reasoning.differentials", {"differentials": float(len(diffs)), "proven_share": _ratio(proven, len(diffs))},
            {"proven_share": 0.8})
        maps = kc.rows(m.Mapping, self.run_id)
        agree = sum(1 for x in maps if x.validation.get("verdict") == "agree")
        cap("mapping", {"proposals": float(len(maps)), "validation_agreement": _ratio(agree, len(maps)),
                        "certified_share": _ratio(sum(x.status == "certified" for x in maps), len(maps))}, {})
        cohorts = kc.rows(m.Cohort, self.run_id)
        cap("data_quality", {"cohorts": float(len(cohorts)),
                             "root_cause_selected": _ratio(sum(1 for c in cohorts if c.root_cause.get("cause_code", "RC-UNRESOLVED")
                                                                 != "RC-UNRESOLVED"), len(cohorts))}, {})
        clusters = kc.rows(m.BreakCluster, self.run_id)
        explained = sum(1 for c in clusters if c.body.get("linked_differential_id")
                        or any(t in c.signature for t in (":missing_in_target", ":rejected_at_load")))
        cap("reconciliation", {"breaks": float(kc.count(m.ReconciliationBreak, self.run_id)),
                               "clusters_explained": _ratio(explained, len(clusters))}, {"clusters_explained": 0.8})
        n_cases = kc.count(m.ConductCase, self.run_id)
        decided_cases = kc.count(m.ConductCase, self.run_id, status="certified") + kc.count(m.ConductCase, self.run_id, status="rejected")
        cap("conduct", {"cases": float(n_cases), "decided": _ratio(decided_cases, n_cases)}, {})
        g_events = kc.rows(m.GuardrailEvent, self.run_id)
        cap("guardrails", {"events": float(len(g_events)), "blocks": float(sum(not g.passed for g in g_events))}, {},
            [f"{sum(not g.passed for g in g_events)} explicit block(s) recorded"])
        hr = [e.record for e in kc.rows(m.HeadroomEvent, self.run_id)]
        violations = sum(1 for r in hr if r["content_type"] in PROTECTED and r["tokens_after"] != r["tokens_before"])
        before = sum(r["tokens_before"] for r in hr)
        cap("headroom", {"protected_integrity_violations": float(violations),
                         "compression_ratio": _ratio(before - sum(r["tokens_after"] for r in hr), before) if before else 0.0},
            {"protected_integrity_violations": 0.0}, maxima={"protected_integrity_violations"})
        ledger = self.ledger.snapshot()
        cost = Decimal(ledger["estimated_cost"])
        cap("cost", {"estimated_cost": float(cost), "llm_calls": float(ledger["llm_calls"]),
                     "budget_used": float(cost / Decimal(str(ledger["budgets"]["estimated_cost"]))) if Decimal(str(ledger["budgets"]["estimated_cost"])) else 0.0},
            {"budget_used": 1.0}, maxima={"budget_used"})
        execs = kc.rows(m.AgentExecution, self.run_id)
        latency = {e.agent_name: e.duration_ms for e in execs}
        cap("latency", {"agent_ms_total": round(sum(latency.values()), 1), "slowest_agent_ms": max(latency.values(), default=0.0)}, {})

        rules = [r for r in kc.rows(m.Rule, self.run_id) if source_kind(r.agent) != "schema"]
        # a rule superseded by a decided adjudication (e.g. the target side when the source was accepted) is resolved
        superseded = {rid for d in diffs if d.status in ("certified", "rejected")
                      for rid in d.body.get("source_rule_ids", []) + d.body.get("target_rule_ids", [])
                      + d.body.get("document_rule_ids", [])}
        counts = {
            "rules_certified": (sum(r.status == ItemStatus.CERTIFIED.value or r.rule_id in superseded for r in rules),
                                len(rules)),
            "differentials_resolved": (sum(d.status in ("certified", "rejected") for d in diffs), len(diffs)),
            "mappings_certified": (sum(x.status == "certified" for x in maps), len(maps)),
            "cohorts_addressed": (sum(c.status in ("certified", "rejected") for c in cohorts), len(cohorts)),
            "breaks_explained": (explained, len(clusters)),
            "conduct_cases_decided": (decided_cases, n_cases),
        }
        core_ok = all(c.passed for c in caps if c.capability in ("provenance", "audit", "tracing", "headroom"))
        r = readiness(counts, core_ok)
        blockers = [f"{c.capability} below floor: {c.metrics}" for c in caps if not c.passed] + r.blockers
        report = AssuranceReport(report_id=new_id("ASR"), run_id=self.run_id, capabilities=caps, cost=ledger,
                                 latency=latency, readiness_score=r.score, passed=all(c.passed for c in caps),
                                 blockers=blockers)
        kc.save_assurance_report(report)
        self.note_mode(ReasoningMode.DETERMINISTIC)
        self.signal(SignalType.ASSURANCE_REPORT, f"assurance {'passed' if report.passed else 'FAILED'}; readiness "
                    f"{report.readiness_score:.2f}", item_refs=[report.report_id],
                    level=SignalLevel.INFO if report.passed else SignalLevel.WARN,
                    data={"failed": [c.capability for c in caps if not c.passed], "readiness": report.readiness_score})
        return AgentOutput(items={"assurance": [report]})
