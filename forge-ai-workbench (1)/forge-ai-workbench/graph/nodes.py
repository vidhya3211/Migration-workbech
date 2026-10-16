"""Built-in pipeline steps. Each step: kill-switch + budget preflight -> graph span -> agent -> guard -> persist.

Steps register themselves (``@step``) with the channels they read and write and, where it matters, an
``applies`` check: a step whose inputs did not arrive in this handover stands down with a signal instead of
running on nothing (the orchestrator decides from what actually arrived, not from a fixed script). Which steps
run, and in what order, comes from the project's pipeline definition (``graph.pipeline``).

Steps are the only place agent output crosses into the knowledge core, and only after the deterministic
guard / provenance guard has re-verified the evidence. Large outputs (breaks, scores, cases) are written to
PostgreSQL in batches and graph state carries only references and counts.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from langgraph.runtime import Runtime
from langgraph.types import Send

from agents.base import AgentFailed
from blackboard.control_plane import validate_signal
from blackboard.identity import agent_class, extractors
from config.settings import use_settings
from db import models as m
from graph.pipeline import WorkbenchContext
from graph.steps import fanout, step
from observability import context
from observability.tracing.tracer import SpanKind, get_tracer
from reasoning.deterministic.naming import canonical_name
from schemas.artefact import Artefact
from schemas.common import ItemStatus, new_id
from schemas.differential import Differential
from schemas.dq import Cohort, RootCauseHypothesis
from schemas.execution import GuardrailResult
from schemas.mapping import DictionaryEntry, MappingProposal
from schemas.recon import BreakCluster
from schemas.rules import CandidateRule, NormalisedRule, RuleKind
from schemas.signals import Signal, SignalLevel, SignalType

Ctx = Runtime[WorkbenchContext]


class RunHalted(RuntimeError):
    """The kill-switch was pulled. The run stops explicitly at the next node boundary."""


def svc(runtime: Ctx):
    return runtime.context.services


def dump(items) -> list[dict]:
    return [i.model_dump(mode="json") for i in items]


def load(model, items: list[dict] | None) -> list:
    return [model.model_validate(i) for i in (items or [])]


def agent(services, name: str, run_id: str):
    """Instantiate a registered agent by name (no step imports an agent class directly)."""
    return agent_class(name)(services, run_id)


@contextmanager
def node_scope(services, run_id: str, name: str) -> Iterator[None]:
    run = services.kc.get_run(run_id)
    if run is not None and run.status in ("halt_requested", "halted"):
        raise RunHalted(f"run {run_id} halted by the kill-switch before {name}")
    services.ledger(run_id).check_time()
    # one correlation chain per run: every node, agent, tool, MCP, LLM and guardrail span shares the run's trace
    with use_settings(services.settings), context.bind(run_id=run_id, trace_id=run.trace_id if run else None,
                                                        agent_id="orchestrator_agent", agent_tier="T3",
                                                        project_id=services.project_id):
        with get_tracer().span(SpanKind.GRAPH, name):
            yield
    services.kc.update_run(run_id, ledger=services.ledger(run_id).snapshot())


def post(services, run_id: str, message: str, signal_type: SignalType = SignalType.ORCHESTRATION,
         level: SignalLevel = SignalLevel.INFO, data: dict | None = None, item_refs: list[str] | None = None) -> None:
    sig = Signal(signal_id=new_id("SIG"), run_id=run_id, agent="orchestrator_agent", tier="T3",
                 signal_type=signal_type, level=level, message=message, data=data or {}, item_refs=item_refs or [],
                 trace_id=context.get("trace_id"))
    validate_signal(sig)
    services.kc.save_signal(sig)


def mark_step(services, run_id: str, name: str) -> None:
    services.kc.mark_step(run_id, name)


def stand_down(services, run_id: str, name: str, reason: str) -> None:
    post(services, run_id, f"{name} stood down - {reason}", data={"step": name, "stood_down": True, "reason": reason})


# ---------------------------------------------------------------- applicability checks (what arrived decides)
def _has_extracts(role: str):
    def check(state, services) -> str | None:
        return None if services.staging.tables(state["run_id"], role) else f"no {role} data extracts in this handover"
    return check


def _has_dictionary(state, services) -> str | None:
    return None if state.get("dictionary") else "no data dictionary (DDL / schema) was recovered"


def _can_map(state, services) -> str | None:
    d = state.get("dictionary") or []
    roles = {e["system"]["system_role"] for e in d}
    return None if {"source", "target"} <= roles else "mapping needs a source and a target data dictionary"


def _can_reconcile(state, services) -> str | None:
    if not state.get("certified_mapping_ids"):
        return "no certified mappings - reconciliation only ever runs through certified mappings"
    for role in ("source", "target"):
        if not services.staging.tables(state["run_id"], role):
            return f"no {role} data extracts to reconcile"
    return None


def _has_breaks(state, services) -> str | None:
    return None if services.kc.count(m.ReconciliationBreak, state["run_id"]) else "no reconciliation breaks"


def _has_customer_impact(state, services) -> str | None:
    """Outcomes come from breaks, or from protected rights the project configured (even with no breaks)."""
    if services.kc.count(m.ReconciliationBreak, state["run_id"]):
        return None
    if (services.settings.heuristics.get("conduct") or {}).get("protected_rights") and state.get("recon_summary"):
        return None
    return "no reconciliation breaks and no protected rights configured"


def _has_outcomes(state, services) -> str | None:
    return None if services.kc.count(m.ConductOutcomeRow, state["run_id"]) else "no customer outcomes to route"


def _has_certified_rules(state, services) -> str | None:
    return None if state.get("certified_rule_ids") else "no certified rules"


def _has_data_extracts(state, services) -> str | None:
    return None if any(a.get("artefact_type") == "data_extract" for a in state.get("artefacts", [])) \
        else "no data extracts in this handover"


# ============================================================================================ discovery
@step("self_test", agent="orchestrator_agent", required=True,
      description="Control-plane and deterministic-guard self-test: prove the refusals before any work runs")
def self_test(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    with node_scope(s, state["run_id"], "control_plane_self_test"):
        s.kc.update_run(state["run_id"], status="running")
        agent(s, "orchestrator_agent", state["run_id"]).execute(action="self_test")
    return {}


@step("ingest", agent="ingest_agent", required=True, reads=("upload_ids", "project_id"), writes=("staged",),
      description="Stream every validated upload into the evidence store; tokenise PII; decode mainframe extracts")
def ingest(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    with node_scope(s, state["run_id"], "ingestion_validation"):
        out = agent(s, "ingest_agent", state["run_id"]).execute(upload_ids=state["upload_ids"],
                                                                project_id=state["project_id"],
                                                                input_refs=state["upload_ids"])
    return {"staged": out.items["staged"]}


@step("cartographer", agent="cartographer_agent", required=True, reads=("staged",), writes=("artefacts",),
      description="Classify every staged file and route it to the extractor that handles its type")
def cartographer(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    with node_scope(s, state["run_id"], "cartographer"):
        out = agent(s, "cartographer_agent", state["run_id"]).execute(staged=state["staged"])
    return {"artefacts": dump(out.items["artefacts"])}


@step("stage_data", agent="ingest_agent", reads=("artefacts",), writes=("staged_extracts",),
      applies=_has_data_extracts, description="Load classified data extracts into PostgreSQL (COPY)")
def stage_data(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    with node_scope(s, state["run_id"], "stage_data"):
        arts = load(Artefact, state.get("artefacts"))
        out = agent(s, "ingest_agent", state["run_id"]).stage(state["project_id"], arts)
    return {"staged_extracts": out.items["staged_extracts"]}


def _dispatch(state) -> list[Send]:
    """Applicability dispatch: one parallel extraction task per artefact routed to a registered extractor."""
    routable = extractors()
    return [Send("extract", {"run_id": state["run_id"], "project_id": state.get("project_id"), "artefact": a})
            for a in state.get("artefacts", []) if a.get("route") in routable]


@fanout("extract", dispatcher=_dispatch, writes=("candidate_parts", "dictionary_parts", "analysis_parts"),
        reducers={"candidate_parts": "append", "dictionary_parts": "append", "analysis_parts": "append"},
        description="Parallel specialist extraction: one task per routable artefact")
def extract(payload: dict, runtime: Ctx) -> dict:
    s = svc(runtime)
    art = Artefact.model_validate(payload["artefact"])
    run_id = payload["run_id"]
    with context.bind(artefact_id=art.artefact_id, system_name=art.system.system_name,
                      system_role=art.system.system_role.value):
        with node_scope(s, run_id, f"extract:{art.route}"):
            try:
                out = agent(s, art.route, run_id).execute(artefact=art, input_refs=[art.blob_uri])
            except AgentFailed as exc:   # one bad file must not sink the run - but it is never silent
                s.kc.set_artefact_analysis(art.artefact_id, "failed", {"error": str(exc)[:500]})
                return {"analysis_parts": [{"artefact_id": art.artefact_id, "status": "failed", "error": str(exc)[:300]}]}
    s.kc.set_artefact_analysis(art.artefact_id, "analysed", out.summary)
    return {"candidate_parts": dump(out.items.get("candidates", [])),
            "dictionary_parts": dump(out.items.get("dictionary", [])),
            "analysis_parts": [{"artefact_id": art.artefact_id, "status": "analysed", "agent": art.route}]}


@step("guard", agent="orchestrator_agent", reads=("candidate_parts", "dictionary_parts", "analysis_parts", "artefacts"),
      writes=("candidates", "accepted_rule_ids", "guard_rejections", "dictionary", "facts"),
      description="Fan-in + deterministic guard: every candidate re-verified against the raw source")
def guard(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "deterministic_guard"):
        candidates = load(CandidateRule, state.get("candidate_parts"))
        outcome = s.guard(run_id).verify_rules(candidates)
        for rule, reason in outcome.rejected:
            s.kc.save_guardrail_event(run_id, rule.agent, GuardrailResult(
                stage="deterministic", policy="deterministic_guard", passed=False, severity="block",
                reason=f"{rule.rule_id} ({rule.name}): {reason}", subject_ref=rule.rule_id))
        dictionary = {d["entry_id"]: d for d in state.get("dictionary_parts", [])}
        routable = extractors()
        arts = state.get("artefacts", [])
        analysed = [a for a in state.get("analysis_parts", []) if a["status"] == "analysed"]
        post(s, run_id, f"deterministic guard accepted {len(outcome.accepted)} / refused {len(outcome.rejected)} "
             "candidate rule(s)", SignalType.GUARDRAIL,
             SignalLevel.WARN if outcome.rejected else SignalLevel.INFO,
             {"accepted": len(outcome.accepted), "refused": [f"{r.rule_id}: {why}" for r, why in outcome.rejected[:20]]})
        for a in arts:
            if a.get("route") not in routable:
                s.kc.set_artefact_analysis(a["artefact_id"], "staged_data" if a["artefact_type"] == "data_extract"
                                           else "unrouted", {})
    return {"candidates": dump(outcome.accepted), "accepted_rule_ids": [r.rule_id for r in outcome.accepted],
            "guard_rejections": [{"rule_id": r.rule_id, "reason": why} for r, why in outcome.rejected],
            "dictionary": list(dictionary.values()),
            "facts": {"rules_candidates": len(candidates), "rules_accepted": len(outcome.accepted),
                      "rules_refused": len(outcome.rejected),
                      "artefacts_routable": sum(1 for a in arts if a.get("route") in routable),
                      "artefacts_analysed": len(analysed)}}


@step("normaliser", agent="normaliser_agent", reads=("candidates", "dictionary"), writes=("normalised",),
      description="Canonicalise every rule (name, domain, confidence)")
def normaliser(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "normaliser"):
        candidates = load(CandidateRule, state.get("candidates"))
        out = agent(s, "normaliser_agent", run_id).execute(candidates=candidates)
        normalised = out.items["normalised"]
        s.kc.save_rules(candidates, {n.rule_id: n.domain for n in normalised})
        s.kc.save_dictionary(run_id, load(DictionaryEntry, state.get("dictionary")))
    return {"normalised": dump(normalised)}


@step("differential", agent="differential_agent", reads=("normalised",), writes=("differentials",),
      description="Prove every source/target disagreement and rank it by exposure")
def differential(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "differential"):
        out = agent(s, "differential_agent", run_id).execute(normalised=load(NormalisedRule, state.get("normalised")))
        verified = s.guard(run_id).verify_items(out.items["differentials"], id_of=lambda d: d.differential_id)
        for d, why in verified.rejected:
            post(s, run_id, f"provenance guard refused differential {d.canonical_name}: {why}", SignalType.GUARDRAIL,
                 SignalLevel.REFUSAL)
        s.kc.save_differentials([(d, None) for d in verified.accepted])
    return {"differentials": dump(verified.accepted)}


@step("adjudication", agent="adjudication_agent", reads=("differentials",), writes=("briefs",),
      description="Brief the accountable expert on every disagreement (decides nothing)")
def adjudication(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "adjudication_brief"):
        out = agent(s, "adjudication_agent", run_id).execute(differentials=load(Differential, state.get("differentials")))
        s.kc.set_differential_briefs(run_id, out.items["briefs"])
    return {"briefs": dump(out.items["briefs"])}


@step("certified_core", agent="orchestrator_agent", reads=("normalised",),
      writes=("certified_rule_ids", "certified_domains"),
      description="The certified knowledge core the rest of the journey builds on")
def certified_view(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "certified_knowledge_core"):
        certified = {r.rule_id for r in s.kc.rows(m.Rule, run_id, status=ItemStatus.CERTIFIED.value)}
        domains: dict[str, dict] = {}
        for n in load(NormalisedRule, state.get("normalised")):
            if n.rule_id in certified and n.kind == RuleKind.CONDITION_NAME:
                row = s.kc.get(m.Rule, n.rule_id)
                parent = (row.context or {}).get("parent")
                values = [v.strip("'\"") for v in (row.context or {}).get("values", [])]
                if parent:
                    d = domains.setdefault(canonical_name(parent), {"values": [], "rule_ids": []})
                    d["values"] = sorted(set(d["values"]) | set(values))
                    d["rule_ids"].append(n.rule_id)
        # one 88-level ("SMOKER = 'Y'") names a condition, not a closed code list; two or more enumerate a domain
        domains = {k: v for k, v in domains.items() if len(v["rule_ids"]) >= 2}
        post(s, run_id, f"certified knowledge core holds {len(certified)} rule(s)", SignalType.CERTIFIED_RULES,
             item_refs=sorted(certified)[:200], data={"certified": len(certified)})
    return {"certified_rule_ids": sorted(certified), "certified_domains": domains}


@step("story", agent="story_agent", reads=("certified_rule_ids", "normalised"), writes=("stories",),
      applies=_has_certified_rules, description="Turn certified rules into testable user stories")
def story(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "story"):
        certified = set(state.get("certified_rule_ids", []))
        rules = [n for n in load(NormalisedRule, state.get("normalised")) if n.rule_id in certified
                 and n.source_kind == "code" and n.system.system_role.value == "source"]
        out = agent(s, "story_agent", run_id).execute(certified=rules)
        s.kc.save_stories(out.items["stories"])
    return {"stories": [{"story_id": x.story_id, "title": x.title} for x in out.items["stories"]]}


# ============================================================================================ mapping & build
@step("mapping", agent="mapping_agent", reads=("dictionary",), writes=("mappings", "unmapped"), applies=_can_map,
      description="Propose attribute-level source-to-target mappings")
def mapping(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "mapping"):
        out = agent(s, "mapping_agent", run_id).execute(dictionary=load(DictionaryEntry, state.get("dictionary")))
        verified = s.guard(run_id).verify_items(out.items["mappings"], evidence_of=lambda p: [p.source.evidence,
                                                                                              p.target.evidence],
                                                id_of=lambda p: p.mapping_id)
        s.kc.save_mappings(verified.accepted)
    return {"mappings": dump(verified.accepted), "unmapped": out.items["unmapped"]}


@step("validation", agent="validation_agent", reads=("mappings", "dictionary"), writes=("validations",),
      description="Independently check every mapping against the real source values")
def validation(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "validation"):
        out = agent(s, "validation_agent", run_id).execute(mappings=load(MappingProposal, state.get("mappings")),
                                                           dictionary=load(DictionaryEntry, state.get("dictionary")))
        for v in out.items["validations"]:
            s.kc.set_mapping_validation(v.mapping_id, v.model_dump(mode="json"))
    return {"validations": dump(out.items["validations"])}


@step("codegen", agent="codegen_agent", reads=("mappings", "certified_mapping_ids"), writes=("generated_code",),
      description="Generate transformation code and tests from certified mappings only")
def codegen(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "codegen"):
        certified = set(state.get("certified_mapping_ids", []))
        ms = [p for p in load(MappingProposal, state.get("mappings")) if p.mapping_id in certified]
        out = agent(s, "codegen_agent", run_id).execute(certified_mappings=ms)
        by_id = {p.mapping_id: p for p in ms}
        for code in out.items["generated_code"]:
            s.kc.save_generated_code(code, [e for mid in code.mapping_ids for e in (by_id[mid].source.evidence,
                                                                                    by_id[mid].target.evidence)])
    return {"generated_code": [{k: v for k, v in c.model_dump(mode="json").items() if k not in ("source", "tests")}
                               for c in out.items["generated_code"]]}


# ============================================================================================ data quality
@step("profiler", agent="profiler_agent", reads=("dictionary",), writes=("profiles",), applies=_has_extracts("source"),
      description="Statistical profile of every source column (computed in PostgreSQL)")
def profiler(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    with node_scope(s, state["run_id"], "profiler"):
        out = agent(s, "profiler_agent", state["run_id"]).execute(dictionary=load(DictionaryEntry, state.get("dictionary")))
    return {"profiles": dump(out.items["profiles"])}


@step("anomaly", agent="anomaly_agent", reads=("dictionary", "certified_domains"), writes=("cohorts",),
      applies=_has_extracts("source"), description="Group records sharing a defect into cohorts")
def anomaly(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "anomaly"):
        out = agent(s, "anomaly_agent", run_id).execute(dictionary=load(DictionaryEntry, state.get("dictionary")),
                                                        certified_domains=state.get("certified_domains", {}))
        verified = s.guard(run_id).verify_items(out.items["cohorts"], id_of=lambda c: c.cohort_id)
        s.kc.save_cohorts(verified.accepted)
    return {"cohorts": dump(verified.accepted)}


@step("root_cause", agent="root_cause_agent", reads=("cohorts",), writes=("root_causes",),
      description="Match each cohort to a known cause (recommendation)")
def root_cause(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "root_cause"):
        out = agent(s, "root_cause_agent", run_id).execute(cohorts=load(Cohort, state.get("cohorts")))
        for h in out.items["root_causes"]:
            s.kc.annotate_cohort(h.cohort_id, "root_cause_agent", root_cause=h.model_dump(mode="json"))
    return {"root_causes": dump(out.items["root_causes"])}


@step("cleansing", agent="cleansing_agent", reads=("cohorts", "root_causes"), writes=("cleansing",),
      description="Propose a fix per cohort (nothing is changed)")
def cleansing(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "cleansing"):
        out = agent(s, "cleansing_agent", run_id).execute(cohorts=load(Cohort, state.get("cohorts")),
                                                          root_causes=load(RootCauseHypothesis, state.get("root_causes")))
        for p in out.items["cleansing"]:
            s.kc.annotate_cohort(p.cohort_id, "cleansing_agent", cleansing=p.model_dump(mode="json"))
    return {"cleansing": dump(out.items["cleansing"])}


@step("live_archive", agent="live_archive_agent", reads=("dictionary",), writes=("live_archive",),
      applies=_has_extracts("source"), description="Score every policy's probability of still being active")
def live_archive(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    with node_scope(s, state["run_id"], "live_archive"):
        out = agent(s, "live_archive_agent", state["run_id"]).execute(
            dictionary=load(DictionaryEntry, state.get("dictionary")))
        s.kc.merge_run_summary(state["run_id"], {"live_archive": out.items["live_archive"][0]})
    return {"live_archive": out.items["live_archive"]}


@step("entity_match", agent="entity_match_agent", reads=("dictionary",), writes=("entity_matches",),
      applies=_has_extracts("source"), description="Match the same customer across records (steward confirms)")
def entity_match(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "entity_match"):
        out = agent(s, "entity_match_agent", run_id).execute(dictionary=load(DictionaryEntry, state.get("dictionary")))
        verified = s.guard(run_id).verify_items(out.items["entity_matches"], id_of=lambda x: x.match_id)
        s.kc.save_entity_matches(verified.accepted)
    return {"entity_matches": {"proposed": len(verified.accepted), "refused": len(verified.rejected)}}


# ============================================================================================ reconciliation & conduct
@step("reconciler", agent="reconciler_agent", reads=("mappings", "certified_mapping_ids", "dictionary"),
      writes=("recon_summary",), applies=_can_reconcile,
      description="Compare loaded target values with source values through certified mappings (in PostgreSQL)")
def reconciler(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    refused: list[int] = [0]

    def sink(batch):
        verified = s.guard(run_id).verify_items(batch, id_of=lambda b: b.break_id)
        refused[0] += len(verified.rejected)
        s.kc.save_breaks(verified.accepted)
    with node_scope(s, run_id, "reconciler"):
        certified = set(state.get("certified_mapping_ids", []))
        ms = [p for p in load(MappingProposal, state.get("mappings")) if p.mapping_id in certified]
        out = agent(s, "reconciler_agent", run_id).execute(certified_mappings=ms, sink=sink,
                                                           dictionary=load(DictionaryEntry, state.get("dictionary")))
        if refused[0]:
            post(s, run_id, f"provenance guard refused {refused[0]} break(s)", SignalType.GUARDRAIL, SignalLevel.REFUSAL)
        s.kc.merge_run_summary(run_id, {"waterfall": out.items["recon_summary"].get("waterfall", [])})
    return {"recon_summary": {**out.items["recon_summary"], "refused": refused[0]}}


def _has_controls(state, services) -> str | None:
    cfg = services.settings.heuristics.get("reconciliation") or {}
    if not any(cfg.get(k) for k in ("derived", "cross_target", "controls")):
        return "no derived values, cross-target checks or control totals configured"
    if not services.staging.tables(state["run_id"], None):
        return "no data extracts staged"
    return None


@step("controls", agent="control_reconciler_agent", reads=("dictionary",), writes=("control_summary",),
      applies=_has_controls, description="Derived values, cross-target drift and control totals (e.g. CASS)")
def controls(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    refused: list[int] = [0]

    def sink(batch):
        verified = s.guard(run_id).verify_items(batch, id_of=lambda b: b.break_id)
        refused[0] += len(verified.rejected)
        s.kc.save_breaks(verified.accepted, agent="control_reconciler_agent")
    with node_scope(s, run_id, "controls"):
        out = agent(s, "control_reconciler_agent", run_id).execute(
            dictionary=load(DictionaryEntry, state.get("dictionary")), sink=sink)
        if refused[0]:
            post(s, run_id, f"provenance guard refused {refused[0]} control break(s)", SignalType.GUARDRAIL,
                 SignalLevel.REFUSAL)
        s.kc.merge_run_summary(run_id, {"controls": out.items["controls"][0]})
    return {"control_summary": {**out.items["controls"][0], "refused": refused[0]}}


@step("break_cluster", agent="break_cluster_agent", reads=("differentials", "normalised", "dictionary"),
      writes=("clusters",), applies=_has_breaks, description="Group breaks by shared root cause (replay-proven)")
def break_cluster(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "break_cluster"):
        decided = {d.differential_id for d in s.kc.rows(m.Differential, run_id) if d.status in ("certified", "rejected")}
        diffs = [d for d in load(Differential, state.get("differentials")) if d.differential_id in decided]
        from agents.reconciliation.break_cluster_agent import cluster_signature
        from schemas.common import stable_id
        groups = {stable_id("CLU", run_id, cluster_signature(g)): g
                  for g in s.kc.break_groups(run_id, s.settings.heuristics.get("break_cluster") or {})}
        out = agent(s, "break_cluster_agent", run_id).execute(
            differentials=diffs, normalised=load(NormalisedRule, state.get("normalised")),
            dictionary=load(DictionaryEntry, state.get("dictionary")), duplicates=_certified_duplicates(s, run_id))
        s.kc.save_clusters(out.items["clusters"], groups)
    return {"clusters": dump(out.items["clusters"])}


@step("lineage", agent="lineage_agent", reads=("clusters", "mappings", "differentials"), writes=("lineage",),
      applies=_has_breaks, description="Trace the exact path a sample of each cluster's values took")
def lineage(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "lineage"):
        clusters = load(BreakCluster, state.get("clusters"))
        per = s.settings.scale.lineage_breaks_per_cluster
        breaks = [b for c in clusters for b in s.kc.breaks_for_cluster(run_id, c.cluster_id, per)]
        out = agent(s, "lineage_agent", run_id).execute(breaks=breaks, clusters=clusters,
                                                        mappings=load(MappingProposal, state.get("mappings")),
                                                        differentials=load(Differential, state.get("differentials")))
        for t in out.items["lineage"]:
            s.kc.set_break_lineage(t.break_id, t.model_dump(mode="json"))
    return {"lineage": {"traced": len(out.items["lineage"]), "clusters": len(clusters)}}


@step("outcome", agent="outcome_agent", reads=("clusters", "mappings", "certified_mapping_ids", "dictionary",
                                               "recon_summary"),
      writes=("outcomes",), applies=_has_customer_impact,
      description="Flag real customer impact from the breaks (flags only)")
def outcome(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "outcome"):
        accepted = {c.cluster_id for c in s.kc.rows(m.BreakCluster, run_id) if c.status == "certified"}
        clusters = load(BreakCluster, state.get("clusters"))
        # breaks the analyst accepted as tolerable variance are not conduct cases
        not_loaded = (":missing_in_target", ":rejected_at_load")
        excluded = {c.cluster_id for c in clusters if c.cluster_id in accepted
                    and not any(t in c.signature for t in not_loaded)
                    and not c.linked_differential_id}
        certified = set(state.get("certified_mapping_ids", []))
        out = agent(s, "outcome_agent", run_id).execute(
            clusters=clusters, excluded_clusters=excluded, duplicates=_certified_duplicates(s, run_id),
            certified_mappings=[p for p in load(MappingProposal, state.get("mappings")) if p.mapping_id in certified],
            dictionary=load(DictionaryEntry, state.get("dictionary")))
    return {"outcomes": out.items["outcomes"][0]}


@step("case_routing", agent="case_routing_agent", reads=("outcomes",), writes=("cases",), applies=_has_outcomes,
      description="Route conduct cases by materiality to the accountable queue")
def case_routing(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    counts = {"saved": 0, "refused": 0}

    def sink(batch):
        verified = s.guard(run_id).verify_items(batch, evidence_of=lambda c: c.outcome.evidence,
                                                id_of=lambda c: c.case_id)
        counts["refused"] += len(verified.rejected)
        counts["saved"] += s.kc.save_conduct_cases(verified.accepted)
    with node_scope(s, run_id, "case_routing"):
        out = agent(s, "case_routing_agent", run_id).execute(sink=sink)
    return {"cases": {**counts, "by_band": out.items["summary"][0]}}


def _certified_duplicates(s, run_id: str) -> dict[str, str]:
    """right_ref -> left_ref for duplicate-customer matches a data steward confirmed."""
    return {x.right_ref: x.left_ref for x in s.kc.rows(m.EntityMatch, run_id, status=ItemStatus.CERTIFIED.value)}


# ============================================================================================ cross-cutting
@step("retrieval", agent="retrieval_agent", writes=("facts",), reducers={"facts": "merge"},
      description="Index the certified core for the grounded assistant")
def retrieval(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    with node_scope(s, state["run_id"], "retrieval_index"):
        out = agent(s, "retrieval_agent", state["run_id"]).execute()
    return {"facts": {"indexed_documents": out.summary.get("documents", 0)}}


@step("assurance", agent="assurance_agent", reads=("facts",), writes=("assurance",),
      description="Score every capability's output quality against the accuracy floors")
def assurance(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    with node_scope(s, state["run_id"], "assurance"):
        out = agent(s, "assurance_agent", state["run_id"]).execute(run_facts=state.get("facts", {}))
    rep = out.items["assurance"][0]
    return {"assurance": {"report_id": rep.report_id, "passed": rep.passed, "readiness": rep.readiness_score,
                          "blockers": rep.blockers[:20]}}


@step("finalize", agent="orchestrator_agent", required=True, reads=("assurance", "go_no_go", "gates"),
      description="Close the run with its summary")
def finalize(state, runtime: Ctx) -> dict:
    s = svc(runtime)
    run_id = state["run_id"]
    with node_scope(s, run_id, "finalize"):
        kc = s.kc
        summary = {
            "artefacts": len(state.get("artefacts", [])), "rules": len(state.get("accepted_rule_ids", [])),
            "differentials": len(state.get("differentials", [])),
            "certified_rules": len(state.get("certified_rule_ids", [])),
            "mappings": len(state.get("mappings", [])), "cohorts": len(state.get("cohorts", [])),
            "breaks": kc.count(m.ReconciliationBreak, run_id), "conduct_cases": kc.count(m.ConductCase, run_id),
            "assurance": state.get("assurance", {}), "go_no_go": state.get("go_no_go", {}),
            "gates": state.get("gates", {}),
        }
        run = kc.get_run(run_id)
        summary = {**((run.summary or {}) if run else {}), **summary}     # keep what steps published (waterfall)
        kc.update_run(run_id, status="completed", current_gate=None, summary=summary,
                      finished_at=datetime.now(timezone.utc))
        post(s, run_id, f"run completed; go/no-go: {state.get('go_no_go', {}).get('decision', 'undecided')}")
    s.release(run_id)
    return {}
