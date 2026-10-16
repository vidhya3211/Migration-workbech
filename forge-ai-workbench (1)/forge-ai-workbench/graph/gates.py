"""Durable human checkpoints built on LangGraph ``interrupt()``, registered as pipeline steps ``gate:<name>``.

A gate: collects the items needing a named human -> writes HITL tasks (idempotent, bulk) -> ``interrupt()``
(the run is checkpointed in PostgreSQL and waits - for days if necessary - without blocking other runs or a
worker) -> on resume, applies each *valid* decision through the human-policy guardrail and the repository's
certification path. Anything not decided, or decided by the wrong role / a non-human, becomes ``unresolved`` -
never approved.

The accountable role a gate represents comes from configuration (``human_roles``; a project may override it).
Which gates a run has, and where, comes from the project's pipeline definition.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from blackboard.control_plane import ControlPlaneViolation
from db import models as m
from graph.nodes import load, node_scope, post, svc
from graph.pipeline import WorkbenchContext
from graph.steps import StepSpec, register_step
from guardrails.pipeline import GuardrailContext, GuardrailViolation, Stage
from guardrails.policy.policies import DecisionSubject
from observability.logging.logger import Event, log_event
from schemas.build import HitlTask
from schemas.common import HumanRole, ItemStatus, stable_id
from schemas.differential import Differential, HumanDecision
from schemas.signals import SignalLevel, SignalType

MAX_INTERRUPT_ITEMS = 50


@dataclass
class GateItem:
    item_id: str
    item_type: str
    title: str
    brief: dict = field(default_factory=dict)


Collect = Callable[[dict, object], list[GateItem]]
Apply = Callable[[dict, object, GateItem, HumanDecision], str]
Outputs = Callable[[dict, object], dict]


@dataclass
class GateSpec:
    name: str
    collect: Collect
    apply: Apply
    outputs: Outputs = lambda state, s: {}
    output_channels: tuple[str, ...] = ()
    default_role: HumanRole = HumanRole.ACTUARY_SME
    reads: tuple[str, ...] = ()
    description: str = ""

    def role(self, settings) -> HumanRole:
        return HumanRole(settings.human_roles.get(self.name, self.default_role.value))


def make_gate(spec: GateSpec):
    def gate(state: dict, runtime: Runtime[WorkbenchContext]) -> dict:
        s = svc(runtime)
        run_id = state["run_id"]
        role = spec.role(s.settings)
        with node_scope(s, run_id, f"gate:{spec.name}"):
            items = spec.collect(state, s)
            tasks = [HitlTask(task_id=stable_id("TSK", run_id, spec.name, it.item_id), run_id=run_id, gate=spec.name,
                              item_type=it.item_type, item_id=it.item_id, required_role=role, title=it.title,
                              brief=it.brief) for it in items]
            created = 0
            if tasks:
                created = s.kc.create_hitl_tasks(tasks)
                s.kc.update_run(run_id, status="awaiting_human", current_gate=spec.name)
            if tasks and created:   # the node re-runs on resume; announce the review only once
                post(s, run_id, f"{len(tasks)} item(s) await the {role.value.replace('_', ' ')} at gate "
                     f"'{spec.name}'", SignalType.HUMAN_REVIEW_REQUIRED,
                     data={"gate": spec.name, "role": role.value, "items": len(tasks)},
                     item_refs=[t.task_id for t in tasks[:500]])
        if not tasks:
            return {"gates": {spec.name: {"items": 0, "decided": 0, "unresolved": 0}}, **spec.outputs(state, s)}
        s.release(run_id)
        resume = interrupt({"gate": spec.name, "role": role.value, "items": len(tasks),
                            "preview": [{"task_id": t.task_id, "item_id": t.item_id, "title": t.title}
                                        for t in tasks[:MAX_INTERRUPT_ITEMS]]})
        with node_scope(s, run_id, f"gate:{spec.name}:apply"):
            decisions: dict = (resume or {}).get("decisions") or {}
            decided, unresolved = 0, []
            ctx = GuardrailContext(run_id, "orchestrator_agent", {"environment": s.settings.environment})
            for t, it in zip(tasks, items):
                raw = decisions.get(t.task_id)
                if raw is None:
                    unresolved.append(it)
                    continue
                try:
                    d = HumanDecision.model_validate(raw)
                    if d.item_id != it.item_id:
                        raise ControlPlaneViolation("decision refers to a different item")
                    s.guardrails.run(Stage.HUMAN_POLICY, DecisionSubject(it.item_type, d), ctx)
                    row = s.kc.get(m.HitlTask, t.task_id)
                    if row is not None and row.status == "pending":
                        s.kc.record_task_decision(t.task_id, d)
                    spec.apply(state, s, it, d)
                    decided += 1
                except (GuardrailViolation, ControlPlaneViolation, ValueError, KeyError) as exc:
                    unresolved.append(it)
                    post(s, run_id, f"decision on {it.item_id} not applied: {exc}", SignalType.HUMAN_REVIEW_REQUIRED,
                         SignalLevel.REFUSAL)
            by_type: dict[str, list[str]] = {}
            for it in unresolved:
                by_type.setdefault(it.item_type, []).append(it.item_id)
            for item_type, ids in by_type.items():
                s.kc.mark_unresolved(run_id, item_type, ids, "no valid human decision when the gate closed")
            s.kc.close_gate_tasks(run_id, spec.name)
            s.kc.update_run(run_id, status="running", current_gate=None)
            log_event(Event.HUMAN_DECISION_RECEIVED, f"gate {spec.name}: {decided} decided, {len(unresolved)} unresolved")
            post(s, run_id, f"gate '{spec.name}' closed: {decided} decided, {len(unresolved)} unresolved",
                 SignalType.HUMAN_REVIEW_REQUIRED, SignalLevel.WARN if unresolved else SignalLevel.INFO,
                 data={"gate": spec.name, "decided": decided, "unresolved": [i.item_id for i in unresolved][:50]})
            out = spec.outputs(state, s)
        return {"gates": {spec.name: {"items": len(tasks), "decided": decided, "unresolved": len(unresolved)}}, **out}
    gate.__name__ = f"gate_{spec.name}"
    return gate


GATES: dict[str, GateSpec] = {}


def register_gate(spec: GateSpec) -> GateSpec:
    """Registers a gate as the pipeline step ``gate:<name>``."""
    register_step(StepSpec(name=f"gate:{spec.name}", fn=make_gate(spec), agent="orchestrator_agent", kind="gate",
                           reads=spec.reads, writes=("gates", *spec.output_channels), reducers={"gates": "merge"},
                           retry=False, description=spec.description or f"Human checkpoint: {spec.name}"))
    GATES[spec.name] = spec
    return spec


# =================================================================================== rule adjudication (Stage 3)
def _collect_rules(state: dict, s) -> list[GateItem]:
    briefs = {b["differential_id"]: b for b in state.get("briefs", [])}
    items, disputed = [], set()
    for d in load(Differential, state.get("differentials")):
        disputed |= set(d.source_rule_ids + d.target_rule_ids + d.document_rule_ids)
        items.append(GateItem(d.differential_id, "differential",
                              f"{d.canonical_name}: {d.difference_type.value.replace('_', ' ')}",
                              {"brief": briefs.get(d.differential_id, {}), "explanation": d.explanation,
                               "severity": d.severity.score, "exposure": d.financial_exposure}))
    agreed = [r for r in state.get("accepted_rule_ids", []) if r not in disputed]
    if agreed:
        items.append(GateItem(f"AGREED-{state['run_id']}", "rule",
                              f"Certify {len(agreed)} rule(s) not in dispute (proven equivalent or uncontested)",
                              {"rule_ids": agreed}))
    return items


def _certify(s, run_id: str, rule_ids: list[str], d: HumanDecision, expr: str | None, diff_id: str | None) -> None:
    for rid in rule_ids:
        row = s.kc.get(m.Rule, rid)
        if row is None or row.status == ItemStatus.CERTIFIED.value:
            continue
        rd = d.model_copy(update={"item_id": rid, "item_type": "rule"})
        s.kc.certify_rule(run_id, rid, rd, expr or row.expression, diff_id)


def _apply_rules(state: dict, s, it: GateItem, d: HumanDecision) -> str:
    run_id = state["run_id"]
    if it.item_type == "rule":
        if d.decision in ("approve", "confirm"):
            _certify(s, run_id, it.brief["rule_ids"], d, None, None)
            return "certified"
        return "rejected"
    diff = next(x for x in load(Differential, state.get("differentials")) if x.differential_id == it.item_id)
    if d.decision == "override" and not (d.certified_expression or "").strip():
        raise ValueError("an override must state the certified expression")
    status = s.kc.apply_human_decision(run_id, "differential", it.item_id, d)
    if d.decision == "accept_source":
        _certify(s, run_id, diff.source_rule_ids, d, None, diff.differential_id)
    elif d.decision == "accept_target":
        _certify(s, run_id, diff.target_rule_ids, d, None, diff.differential_id)
    elif d.decision == "override":
        # the human states the rule: certify one representative rule carrying that expression; the other rules in
        # the dispute are superseded by the decision (resolved, but not certified as written)
        anchor = (diff.source_rule_ids or diff.target_rule_ids)[:1]
        _certify(s, run_id, anchor, d, d.certified_expression, diff.differential_id)
    return status


# =================================================================================== Stage 6 gates
def _collect_mappings(state: dict, s) -> list[GateItem]:
    vals = {v["mapping_id"]: v for v in state.get("validations", [])}
    return [GateItem(mp["mapping_id"], "mapping",
                     f"{mp['source']['entity']}.{mp['source']['field']} -> {mp['target']['entity']}.{mp['target']['field']}",
                     {"score": mp["score"], "transformation": mp["transformation"], "alternatives": mp["alternatives"],
                      "reasoning_mode": mp["reasoning_mode"], "validation": vals.get(mp["mapping_id"], {})})
            for mp in state.get("mappings", [])]


def _mapping_outputs(state: dict, s) -> dict:
    return {"certified_mapping_ids": [r.mapping_id for r in s.kc.rows(m.Mapping, state["run_id"],
                                                                      status=ItemStatus.CERTIFIED.value)]}


def _collect_dq(state: dict, s) -> list[GateItem]:
    run_id = state["run_id"]
    items = [GateItem(x.match_id, "entity_match",
                      f"Same customer? {x.left_ref} ~ {x.right_ref} ({x.body.get('grade', 'ambiguous')} match)",
                      {"score": x.body["score"]["score"], "components": x.body["score"]["score_components"],
                       "grade": x.body.get("grade"), "blocked_on": x.body.get("blocked_on", []),
                       "conflicts": x.body.get("conflicts", []), "evidence_notes": x.body["score"].get("evidence", [])})
             for x in s.kc.rows(m.EntityMatch, run_id, order_by=m.EntityMatch.match_id)]
    rc = {r["cohort_id"]: r for r in state.get("root_causes", [])}
    cl = {c["cohort_id"]: c for c in state.get("cleansing", [])}
    items += [GateItem(c["cohort_id"], "cohort", f"{c['entity']}.{c['column']}: {c['defect_code']} "
                       f"({c['record_count']} record(s))",
                       {"description": c["description"], "root_cause": rc.get(c["cohort_id"], {}),
                        "cleansing": cl.get(c["cohort_id"], {}), "records": c["record_refs"][:20]})
              for c in state.get("cohorts", [])]
    return items


def _collect_clusters(state: dict, s) -> list[GateItem]:
    return [GateItem(c["cluster_id"], "break_cluster", f"{c['signature']} x{c['count']}",
                     {"hypothesis": c["hypothesis"], "linked_rule": c.get("linked_rule"), "total_delta": c["total_delta"],
                      "model_hypothesis": c.get("model_hypothesis")})
            for c in state.get("clusters", [])]


def _collect_cases(state: dict, s) -> list[GateItem]:
    return [GateItem(c.case_id, "conduct_case", f"{c.record_key}: {c.impact_type} ({c.band})",
                     {"annualised_detriment": c.body["outcome"]["annualised_detriment"],
                      "rationale": c.body["outcome"]["rationale"], "route": c.route, "priority": c.priority,
                      "impact_note": c.body.get("impact_note")})
            for c in s.kc.rows(m.ConductCase, state["run_id"], order_by=m.ConductCase.priority)]


def _collect_go(state: dict, s) -> list[GateItem]:
    a = state.get("assurance", {})
    return [GateItem(state["run_id"], "go_no_go", f"Go / no-go for run {state['run_id']}",
                     {"assurance_passed": a.get("passed"), "readiness": a.get("readiness"),
                      "blockers": a.get("blockers", []), "gates": state.get("gates", {})})]


def _apply_simple(item_type: str) -> Apply:
    def apply(state: dict, s, it: GateItem, d: HumanDecision) -> str:
        return s.kc.apply_human_decision(state["run_id"], item_type, it.item_id, d)
    return apply


def _go_outputs(state: dict, s) -> dict:
    task = s.kc.task_for_item("go_no_go", state["run_id"])
    if task is None or task.decision is None:
        return {"go_no_go": {"decision": "unresolved"}}
    return {"go_no_go": {"decision": task.decision["decision"], "decided_by": task.decision["decided_by"],
                         "rationale": task.decision.get("rationale", "")}}


register_gate(GateSpec("rule_adjudication", _collect_rules, _apply_rules, default_role=HumanRole.ACTUARY_SME,
                       reads=("differentials", "briefs", "accepted_rule_ids"),
                       description="Accountable expert certifies the correct rule for every disagreement"))
register_gate(GateSpec("mapping_approval", _collect_mappings, _apply_simple("mapping"), _mapping_outputs,
                       output_channels=("certified_mapping_ids",), default_role=HumanRole.DATA_STEWARD,
                       reads=("mappings", "validations"), description="Data steward approves every field mapping"))
register_gate(GateSpec("data_quality_review", _collect_dq,
                       lambda st, s, it, d: _apply_simple(it.item_type)(st, s, it, d),
                       default_role=HumanRole.DATA_STEWARD, reads=("cohorts", "root_causes", "cleansing"),
                       description="Data steward confirms duplicate customers and defect cohorts"))
register_gate(GateSpec("reconciliation_review", _collect_clusters, _apply_simple("break_cluster"),
                       default_role=HumanRole.RECONCILIATION_ANALYST, reads=("clusters",),
                       description="Reconciliation analyst decides variance vs. real problem per break cluster"))
register_gate(GateSpec("conduct_review", _collect_cases, _apply_simple("conduct_case"),
                       default_role=HumanRole.CONDUCT_OFFICER,
                       description="Conduct officer decides remediation for every affected customer"))
register_gate(GateSpec("go_no_go", _collect_go, _apply_simple("go_no_go"), _go_outputs,
                       output_channels=("go_no_go",), default_role=HumanRole.MIGRATION_LEAD,
                       reads=("assurance", "gates"), description="Migration lead decides whether it is safe to cut over"))
