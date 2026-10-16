"""Service container: builds every platform component from configuration, once per process.

``Services.for_project`` returns the same container viewed through one project's *effective* settings (the
platform settings with the project's overrides layered on top). Graph runs, assistant questions and uploads
always use a project-scoped view, so business vocabulary, heuristics and thresholds are per project while
connections, pools and caches are shared.
"""
from __future__ import annotations

import dataclasses
import threading
from dataclasses import dataclass, field

from config.settings import Settings, effective_settings
from context.headroom.optimizer import HeadroomOptimizer
from context.headroom.store import ContextStore
from db.repository import KnowledgeCore
from db.session import make_engine, make_session_factory
from guardrails.deterministic.evidence import EvidenceVerifier
from guardrails.deterministic.guard import DeterministicGuard
from guardrails.factory import build_pipeline
from guardrails.pipeline import GuardrailContext, GuardrailPipeline
from ingestion.evidence_store import EvidenceReader, EvidenceStore
from observability.cost.ledger import RunLedger
from observability.logging.logger import configure_logging
from observability.metrics.metrics import metrics
from observability.tracing.tracer import Span, get_tracer
from reasoning.llm.factory import build_provider
from reasoning.llm.gateway import LLMGateway
from reasoning.llm.provider import LLMProvider
from retrieval.certified import CertifiedRetrieval
from schemas.artefact import Artefact
from schemas.common import SystemRef, SystemRole
from schemas.execution import GuardrailResult, HeadroomMetrics, LLMCallRecord
from staging.extracts import ExtractStaging
from storage.objects import ObjectStore, build_object_store
from tools.mcp.gateway import MCPGateway
from tools.mcp.registry import MCPRegistry
from tools.mcp.servers import build_devops_server, build_git_server
from tools.native.builtin import build_native_tools
from tools.native.registry import ToolRouter


class RepositorySpanSink:
    """Persists spans for the console drill-down (Run -> Graph -> Agent -> ... -> Database)."""

    def __init__(self, kc: KnowledgeCore) -> None:
        self.kc = kc

    def export(self, span: Span) -> None:
        if span.run_id:
            self.kc.save_span(span)


@dataclass
class _Shared:
    """Per-process state shared by every project-scoped view."""
    ledgers: dict[str, RunLedger] = field(default_factory=dict)
    artefacts: dict[str, dict[str, Artefact]] = field(default_factory=dict)
    project_settings: dict[tuple[str, str], Settings] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)


@dataclass
class Services:
    settings: Settings
    kc: KnowledgeCore
    objects: ObjectStore
    store: EvidenceStore
    reader: EvidenceReader
    staging: ExtractStaging
    guardrails: GuardrailPipeline
    tools: ToolRouter
    mcp: MCPGateway
    llm: LLMGateway
    headroom: HeadroomOptimizer
    retrieval: CertifiedRetrieval
    span_sink: RepositorySpanSink
    engine: object
    platform_settings: Settings | None = None
    project_id: str | None = None
    shared: _Shared = field(default_factory=_Shared)

    # ---------------------------------------------------------------- project scoping
    def for_project(self, project_id: str) -> "Services":
        proj = self.kc.get_project(project_id)
        if proj is None:
            raise KeyError(f"project {project_id} not found")
        base = self.platform_settings or self.settings
        key = (project_id, proj.updated_at.isoformat() if proj.updated_at else "")
        with self.shared.lock:
            eff = self.shared.project_settings.get(key)
            if eff is None:
                eff = effective_settings(base, proj.settings_overrides)
                self.shared.project_settings = {k: v for k, v in self.shared.project_settings.items()
                                                if k[0] != project_id}
                self.shared.project_settings[key] = eff
        return dataclasses.replace(self, settings=eff, platform_settings=base, project_id=project_id)

    def for_run(self, run_id: str) -> "Services":
        project_id = self.kc.run_project(run_id)
        if project_id is None:
            raise KeyError(f"run {run_id} not found")
        return self.for_project(project_id)

    # ---------------------------------------------------------------- per-run helpers
    def ledger(self, run_id: str) -> RunLedger:
        with self.shared.lock:
            led = self.shared.ledgers.get(run_id)
            if led is None:
                led = RunLedger(run_id, self.settings.budgets)
                run = self.kc.get_run(run_id)
                snap = (run.ledger if run else None) or {}
                for key in ("llm_calls", "input_tokens", "compressed_input_tokens", "output_tokens", "cached_tokens",
                            "mcp_calls", "tool_calls", "ats_branches"):
                    setattr(led, key, int(snap.get(key, 0)))
                self.shared.ledgers[run_id] = led
            return led

    def artefacts(self, run_id: str) -> dict[str, Artefact]:
        with self.shared.lock:
            cached = self.shared.artefacts.get(run_id)
        if cached is not None:
            return cached
        rows = self.kc.list_artefacts(run_id)
        out = {r.artefact_id: Artefact(
            artefact_id=r.artefact_id, run_id=r.run_id, upload_id=r.upload_id,
            system=SystemRef(system_name=r.system_name, system_role=SystemRole(r.system_role)), path=r.path,
            artefact_type=r.artefact_type, language=r.language, encoding=r.encoding, sha256=r.sha256,
            size_bytes=r.size_bytes, line_count=r.line_count, confidence=r.confidence, reasons=r.reasons,
            route=r.route, blob_uri=r.blob_uri, classification=r.classification, ingest=r.ingest or {}) for r in rows}
        with self.shared.lock:
            self.shared.artefacts[run_id] = out
        return out

    def invalidate(self, run_id: str) -> None:
        with self.shared.lock:
            self.shared.artefacts.pop(run_id, None)

    def release(self, run_id: str) -> None:
        """Drop per-run caches once a run is finished or parked at a gate."""
        with self.shared.lock:
            self.shared.artefacts.pop(run_id, None)
            self.shared.ledgers.pop(run_id, None)

    def verifier(self, run_id: str) -> EvidenceVerifier:
        return EvidenceVerifier(self.reader, self.artefacts(run_id), self.staging)

    def guard(self, run_id: str) -> DeterministicGuard:
        return DeterministicGuard(self.verifier(run_id))


def build_services(settings: Settings, *, llm_provider: LLMProvider | None = None) -> Services:
    configure_logging(settings.observability.log_level, settings.observability.log_payloads)
    if settings.observability.otel_enabled:
        get_tracer().enable_otel(settings.observability.service_name)
        metrics.enable_otel()
    engine = make_engine(settings)
    kc = KnowledgeCore(make_session_factory(engine, migrate=settings.storage.auto_migrate))
    objects = build_object_store(settings)
    store = EvidenceStore(objects, settings.storage.evidence_container)
    reader = EvidenceReader(store)
    staging = ExtractStaging(engine, copy_batch_rows=settings.ingestion.copy_batch_rows,
                             read_batch_rows=settings.scale.read_batch_rows)

    def record_guardrail(ctx: GuardrailContext, res: GuardrailResult) -> None:
        if ctx.run_id and ctx.run_id != "-":
            kc.save_guardrail_event(ctx.run_id, ctx.agent, res)

    guardrails = build_pipeline(settings, recorder=record_guardrail)
    headroom = HeadroomOptimizer(settings.headroom, ContextStore(objects, settings.storage.context_container),
                                 settings.environment)

    def record_llm(rec: LLMCallRecord, hm: list[HeadroomMetrics]) -> None:
        kc.save_llm_call(rec)
        for x in hm:
            kc.save_headroom_event(rec.run_id, rec.agent, x)
        metrics.inc("workbench_llm_calls", agent=rec.agent, status=rec.status)
        metrics.observe("workbench_llm_input_tokens", rec.compressed_input_tokens, agent=rec.agent)

    llm = LLMGateway(settings, llm_provider or build_provider(settings), headroom, guardrails, recorder=record_llm)
    registry = MCPRegistry(settings.mcp, {
        "git": build_git_server(reader),
        "devops": build_devops_server(objects, settings.storage.context_container),
    })

    def record_mcp(inv, res, outcome: str) -> None:
        kc.save_mcp_invocation(inv.run_id, inv.agent, inv.tool.split(".", 1)[0], inv.tool, res.status, {
            "correlation_id": inv.correlation_id, "arguments_ref": inv.arguments_ref, "output_ref": res.output_ref,
            "latency_ms": res.latency_ms, "error": res.error, "warnings": res.warnings, "outcome": outcome,
            "approval_ref": inv.approval_ref})

    mcp = MCPGateway(registry, guardrails, recorder=record_mcp,
                     injection_threshold=settings.guardrails.injection_block_threshold)
    tools = ToolRouter(build_native_tools(reader), guardrails, mcp)
    span_sink = RepositorySpanSink(kc)
    get_tracer().add_sink(span_sink)
    return Services(settings=settings, kc=kc, objects=objects, store=store, reader=reader, staging=staging,
                    guardrails=guardrails, tools=tools, mcp=mcp, llm=llm, headroom=headroom,
                    retrieval=CertifiedRetrieval(kc, engine, settings.retrieval, environment=settings.environment),
                    span_sink=span_sink, engine=engine, platform_settings=settings)
