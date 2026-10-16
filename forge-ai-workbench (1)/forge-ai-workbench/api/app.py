"""FastAPI application implementing ``api/openapi.yaml`` (a contract test keeps the implementation and the spec in
lockstep). The console never writes to the database - only through here.

Everything below ``/v1/projects/{project_id}`` is scoped to that project; run-level resources check that the run
belongs to a project. List endpoints are paginated; nothing reads across projects. Only the administrator signs
in (``/v1/auth/login``); decisions are attributed to the signed-in administrator and recorded under the
accountable role of the checkpoint they were taken at.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from api.auth import AdminAuthenticator, AuthConfigError, Principal, principal_dependency
from blackboard.control_plane import DECISION_ROLES, ControlPlaneViolation, InvalidDecision
from blackboard.identity import AGENTS, agent_class
from config.settings import Settings, get_settings
from db import models as m
from db.repository import NotFound, ScopeRequired
from graph.pipeline import available_pipelines
from graph.steps import all_steps, describe
from schemas.common import HumanRole
from schemas.differential import HumanDecision
from services.run_service import RunError, RunService
from services.runtime import Services, build_services

SPEC_PATH = Path(__file__).with_name("openapi.yaml")
TERMINAL = {"completed", "halted", "failed", "blocked", "budget_stopped"}


# ------------------------------------------------------------------ request models
class LoginIn(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=500)


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    business_domain: str
    source_technologies: list[str] = Field(default_factory=list)
    target_technologies: list[str] = Field(default_factory=list)
    description: str = Field(default="", max_length=4000)
    settings_overrides: dict | None = None
    pipeline: dict | None = None


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    business_domain: str | None = None
    source_technologies: list[str] | None = None
    target_technologies: list[str] | None = None
    description: str | None = Field(default=None, max_length=4000)
    status: str | None = None
    settings_overrides: dict | None = None
    pipeline: dict | None = None


class UploadIn(BaseModel):
    system_name: str = Field(min_length=1, max_length=200)
    system_role: str
    filename: str = Field(min_length=1, max_length=500)
    size_bytes: int = Field(gt=0)


class StartRun(BaseModel):
    upload_ids: list[str] = Field(min_length=1)


class DecisionIn(BaseModel):
    decision: str
    rationale: str = Field(default="", max_length=4000)
    certified_expression: str | None = None


class QuestionIn(BaseModel):
    question: str = Field(max_length=2000)
    run_id: str | None = None


def _row(obj: Any, exclude: set[str] | None = None) -> dict:
    return {c.name: getattr(obj, c.key) for c in obj.__table__.columns
            if c.name not in (exclude or set()) and hasattr(obj, c.key)}


def create_app(settings: Settings | None = None, services: Services | None = None,
               run_service: RunService | None = None, *, start_workers: bool | None = None) -> FastAPI:
    settings = settings or get_settings()
    services = services or build_services(settings)
    runs = run_service or RunService(services)
    kc = services.kc
    try:
        authenticator = AdminAuthenticator(settings, kc) if settings.auth.mode == "admin" else None
    except AuthConfigError:
        if settings.environment == "production":
            raise
        authenticator = None
    app = FastAPI(title="AI Workbench API", version="2.0.0", docs_url="/v1/docs", openapi_url="/v1/openapi.json")
    app.add_middleware(CORSMiddleware, allow_origins=settings.api.cors_origins, allow_methods=["*"],
                       allow_headers=["*"])
    app.state.services, app.state.runs = services, runs
    who = principal_dependency(settings, authenticator)
    r = APIRouter(prefix="/v1")
    page_default, page_max = settings.api.default_page_size, settings.api.max_page_size

    if start_workers if start_workers is not None else settings.jobs.embedded_worker:
        from services.jobs import start_embedded_workers
        app.state.workers = start_embedded_workers(kc, settings.jobs, runs.handlers)

    # ------------------------------------------------------------------ helpers
    def page(limit: int | None) -> int:
        return max(1, min(limit or page_default, page_max))

    def project_or_404(project_id: str) -> m.Project:
        proj = kc.get_project(project_id)
        if proj is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
        return proj

    def run_or_404(run_id: str, project_id: str | None = None) -> m.IngestRun:
        run = kc.get_run(run_id)
        if run is None or (project_id is not None and run.project_id != project_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
        return run

    def scope(project_id: str, run_id: str | None) -> dict:
        project_or_404(project_id)
        if run_id:
            run_or_404(run_id, project_id)
            return {"run_id": run_id}
        return {"project_id": project_id}

    def decide(item_type: str, item_id: str, body: DecisionIn, p: Principal, project_id: str) -> dict:
        task = kc.task_for_item(item_type, item_id)
        if task is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no review task for {item_type} {item_id}")
        run_or_404(task.run_id, project_id)
        return decide_task(task, body, p)

    def decide_task(task: m.HitlTask, body: DecisionIn, p: Principal) -> dict:
        role = HumanRole(task.required_role) if task.required_role else DECISION_ROLES[task.item_type]
        decision = HumanDecision(item_id=task.item_id, item_type=task.item_type, decision=body.decision,
                                 decided_by=p.user, role=role, rationale=body.rationale,
                                 certified_expression=body.certified_expression)
        try:
            return runs.decide_task(task.task_id, decision)
        except InvalidDecision as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        except (RunError, ControlPlaneViolation) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    # ------------------------------------------------------------------ platform + auth
    @r.get("/health")
    def health() -> dict:
        return {"status": "ok", "environment": settings.environment, "llm_provider": settings.llm.provider,
                "jobs": settings.jobs.backend, "object_store": settings.storage.object_store,
                "auth": settings.auth.mode, "admin_configured": bool(authenticator and authenticator.configured)}

    @r.post("/auth/login")
    def login(body: LoginIn) -> dict:
        if authenticator is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "administrator sign-in is not configured")
        return authenticator.login(body.username, body.password)

    @r.get("/auth/me")
    def me(p: Principal = Depends(who)) -> dict:
        return {"username": p.user, "roles": sorted(p.roles)}

    @r.get("/meta")
    def meta(p: Principal = Depends(who)) -> dict:
        b = settings.branding
        return {"organisation_name": b.organisation_name, "product_name": b.product_name,
                "business_domains": b.business_domains, "pipelines": sorted(available_pipelines()),
                "default_pipeline": settings.pipeline, "human_roles": settings.human_roles,
                "upload_chunk_bytes": settings.ingestion.chunk_bytes, "max_upload_bytes": settings.ingestion.max_zip_bytes}

    @r.get("/agents")
    def agents(p: Principal = Depends(who)) -> list[dict]:
        return [{"name": a.name, "tier": a.tier.value, "group": a.group, "job": a.job,
                 "signals": sorted(s.value for s in a.allowed_signal_types), "tools": sorted(a.allowed_tools),
                 "mcp_tools": sorted(a.allowed_mcp_tools), "llm_purposes": sorted(a.llm_purposes),
                 "uses_ats": a.uses_ats, "extractor": a.extractor, "handles": sorted(a.handles),
                 "module": agent_class(a.name).__module__} for a in AGENTS.values()]

    @r.get("/pipelines")
    def pipelines(p: Principal = Depends(who)) -> dict:
        return {"pipelines": available_pipelines(), "steps": [describe(s) for s in all_steps().values()]}

    # ------------------------------------------------------------------ portfolio + projects
    @r.get("/portfolio")
    def portfolio(p: Principal = Depends(who)) -> dict:
        projects = kc.list_projects()
        latest = kc.latest_runs()
        rows, readiness_vals = [], []
        crit_total = high_total = pending_total = low_conf_total = in_assessment = 0
        for proj in projects:
            run = latest.get(proj.project_id)
            row = {"project_id": proj.project_id, "name": proj.name, "business_domain": proj.business_domain,
                   "source_technologies": proj.source_technologies, "target_technologies": proj.target_technologies,
                   "stage": "upload", "progress": 0.0, "readiness": None, "risks": 0, "run_id": None,
                   "run_status": None, "pending_approvals": 0}
            if kc.list_uploads(proj.project_id) and run is None:
                row["stage"] = "ready"
            if run is not None:
                row.update(run_id=run.run_id, run_status=run.status,
                           progress=round(len(run.steps_completed or []) / run.steps_total, 4) if run.steps_total else 0.0)
                row["stage"] = {"awaiting_human": "review", "completed": "complete"}.get(run.status, "assessment")
                if run.status in TERMINAL - {"completed"}:
                    row["stage"] = run.status
                rep = kc.rows(m.AssuranceReport, run.run_id, order_by=m.AssuranceReport.created_at.desc(), limit=1)
                if rep:
                    row["readiness"] = rep[0].readiness_score
                    readiness_vals.append(rep[0].readiness_score)
                high = kc.count(m.Differential, run.run_id) and sum(
                    1 for d in kc.rows(m.Differential, run.run_id) if d.severity_score >= 0.7)
                high_cases = kc.count(m.ConductCase, run.run_id, band="high")
                row["risks"] = int(high or 0) + high_cases
                crit_total += row["risks"]
                high_total += int(high or 0)
                pending = kc.list_tasks(run.run_id, status="pending")
                row["pending_approvals"] = len(pending)
                pending_total += len(pending)
                low_conf_total += sum(1 for t in pending if float((t.brief or {}).get("score", 1) or 1) < 0.7)
                in_assessment += row["stage"] in ("assessment", "review")
            rows.append(row)
        return {"kpis": {"active_projects": len(projects), "in_assessment": in_assessment,
                         "average_readiness": round(sum(readiness_vals) / len(readiness_vals), 4) if readiness_vals else None,
                         "assessed": len(readiness_vals), "critical_risks": crit_total, "high_severity": high_total,
                         "pending_approvals": pending_total, "low_confidence": low_conf_total},
                "projects": rows}

    @r.get("/projects")
    def list_projects(include_archived: bool = False, p: Principal = Depends(who)) -> list[dict]:
        return [_row(x) for x in kc.list_projects(include_archived)]

    @r.post("/projects", status_code=201)
    def create_project(body: ProjectIn, p: Principal = Depends(who)) -> dict:
        try:
            proj = runs.create_project(name=body.name, business_domain=body.business_domain,
                                       source_technologies=body.source_technologies,
                                       target_technologies=body.target_technologies, description=body.description,
                                       user=p.user, settings_overrides=body.settings_overrides, pipeline=body.pipeline)
        except RunError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        return _row(proj)

    @r.get("/projects/{project_id}")
    def get_project(project_id: str, p: Principal = Depends(who)) -> dict:
        return _row(project_or_404(project_id))

    @r.patch("/projects/{project_id}")
    def patch_project(project_id: str, body: ProjectPatch, p: Principal = Depends(who)) -> dict:
        project_or_404(project_id)
        try:
            return _row(runs.update_project(project_id, p.user, **body.model_dump(exclude_unset=True)))
        except RunError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    @r.get("/projects/{project_id}/overview")
    def project_overview(project_id: str, p: Principal = Depends(who)) -> dict:
        proj = project_or_404(project_id)
        runs_ = kc.list_runs(project_id, limit=10)
        latest = runs.status(runs_[0].run_id) if runs_ else None
        counts = {}
        if runs_:
            rid = runs_[0].run_id
            counts = {"artefacts": kc.count(m.Artefact, rid), "rules": kc.count(m.Rule, rid),
                      "certified_rules": kc.count(m.Rule, rid, status="certified"),
                      "differentials": kc.count(m.Differential, rid), "mappings": kc.count(m.Mapping, rid),
                      "cohorts": kc.count(m.Cohort, rid), "breaks": kc.count(m.ReconciliationBreak, rid),
                      "conduct_cases": kc.count(m.ConductCase, rid),
                      "pending_reviews": len(kc.list_tasks(rid, status="pending"))}
        return {"project": _row(proj), "latest_run": latest, "counts": counts,
                "runs": [_row(x, {"pipeline", "ledger", "summary"}) for x in runs_],
                "uploads": [_row(u, {"manifest"}) for u in kc.list_uploads(project_id)[:20]]}

    # ------------------------------------------------------------------ uploads (chunked)
    @r.get("/projects/{project_id}/uploads")
    def list_uploads(project_id: str, p: Principal = Depends(who)) -> list[dict]:
        project_or_404(project_id)
        return [_row(u) for u in kc.list_uploads(project_id)]

    @r.post("/projects/{project_id}/uploads", status_code=201)
    def begin_upload(project_id: str, body: UploadIn, p: Principal = Depends(who)) -> dict:
        project_or_404(project_id)
        try:
            up = runs.begin_upload(project_id, system_name=body.system_name, system_role=body.system_role,
                                   filename=body.filename, size_bytes=body.size_bytes, user=p.user)
        except RunError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        return _row(up)

    @r.put("/uploads/{upload_id}/chunks/{index}")
    async def put_chunk(upload_id: str, index: int, request: Request, p: Principal = Depends(who)) -> dict:
        data = bytearray()
        async for buf in request.stream():
            data.extend(buf)
            if len(data) > settings.api.max_chunk_bytes:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "chunk too large")
        try:
            return await asyncio.to_thread(runs.put_chunk, upload_id, index, bytes(data))
        except RunError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    @r.post("/uploads/{upload_id}/complete", status_code=202)
    def complete_upload(upload_id: str, p: Principal = Depends(who)) -> dict:
        try:
            return _row(runs.complete_upload(upload_id, p.user))
        except RunError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    @r.get("/uploads/{upload_id}")
    def get_upload(upload_id: str, p: Principal = Depends(who)) -> dict:
        up = kc.get_upload(upload_id)
        if up is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "upload not found")
        return _row(up)

    # ------------------------------------------------------------------ runs
    @r.get("/projects/{project_id}/runs")
    def list_runs(project_id: str, limit: int | None = None, offset: int = 0, p: Principal = Depends(who)) -> list[dict]:
        project_or_404(project_id)
        return [_row(x, {"ledger"}) for x in kc.list_runs(project_id, limit=page(limit), offset=offset)]

    @r.post("/projects/{project_id}/runs", status_code=202)
    def start_run(project_id: str, body: StartRun, p: Principal = Depends(who)) -> dict:
        project_or_404(project_id)
        try:
            return {"run_id": runs.start_run(project_id, body.upload_ids, p.user)}
        except RunError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    @r.get("/runs/{id}")
    def run_status(id: str, p: Principal = Depends(who)) -> dict:
        run_or_404(id)
        return runs.status(id)

    @r.get("/runs/{id}/events")
    async def events(id: str, request: Request, after: int = 0, once: bool = False, p: Principal = Depends(who)):
        run_or_404(id)

        async def stream():
            seq = after
            idle = 0
            while True:
                rows = await asyncio.to_thread(kc.list_signals, id, seq, 500)
                for s in rows:
                    seq = s.seq
                    doc = {k: v for k, v in _row(s).items() if k != "data"} | {"data": s.data}
                    yield f"id: {s.seq}\nevent: {s.signal_type}\ndata: {json.dumps(doc, default=str)}\n\n"
                run = await asyncio.to_thread(kc.get_run, id)
                if once or (not rows and run is not None and run.status in TERMINAL | {"awaiting_human"} and idle > 2):
                    yield f"event: end\ndata: {json.dumps({'status': run.status if run else None})}\n\n"
                    return
                if await request.is_disconnected():
                    return
                idle = 0 if rows else idle + 1
                if not rows:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(1.0)
        return StreamingResponse(stream(), media_type="text/event-stream")

    @r.post("/runs/{id}/halt", status_code=202)
    def halt(id: str, p: Principal = Depends(who)) -> dict:
        run_or_404(id)
        runs.halt(id, p.user)
        return {"run_id": id, "status": "halt_requested"}

    @r.post("/runs/{id}/resume", status_code=202)
    def resume(id: str, p: Principal = Depends(who)) -> dict:
        run_or_404(id)
        gate = runs.current_gate(id)
        if gate is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "run is not waiting at a human gate")
        job_id = runs.resume(id, closed_by=p.user)
        return {"run_id": id, "gate": gate, "status": "resume_queued", "job_id": job_id}

    @r.post("/runs/{id}/go-no-go")
    def go_no_go(id: str, body: DecisionIn, p: Principal = Depends(who)) -> dict:
        run = run_or_404(id)
        return decide("go_no_go", id, body, p, run.project_id)

    @r.get("/runs/{id}/artefacts")
    def artefacts(id: str, limit: int | None = None, offset: int = 0, p: Principal = Depends(who)) -> list[dict]:
        run_or_404(id)
        return [_row(a) for a in kc.rows(m.Artefact, id, order_by=m.Artefact.path, limit=page(limit), offset=offset)]

    @r.get("/runs/{id}/trace")
    def trace(id: str, limit: int | None = None, offset: int = 0, p: Principal = Depends(who)) -> list[dict]:
        run_or_404(id)
        return [_row(s) for s in kc.rows(m.TraceSpan, id, order_by=m.TraceSpan.start_time, limit=page(limit),
                                         offset=offset)]

    @r.get("/runs/{id}/executions")
    def executions(id: str, p: Principal = Depends(who)) -> list[dict]:
        run_or_404(id)
        return [x.record for x in kc.rows(m.AgentExecution, id, order_by=m.AgentExecution.started_at)]

    @r.get("/runs/{id}/costs")
    def costs(id: str, p: Principal = Depends(who)) -> dict:
        run = run_or_404(id)
        return {"ledger": run.ledger or {}, "llm_calls": [c.record for c in kc.rows(m.LLMCall, id)],
                "headroom": [e.record | {"agent": e.agent} for e in kc.rows(m.HeadroomEvent, id)],
                "mcp": [x.record | {"tool": x.tool, "agent": x.agent, "status": x.status}
                        for x in kc.rows(m.MCPInvocation, id)]}

    @r.get("/runs/{id}/guardrails")
    def guardrails(id: str, p: Principal = Depends(who)) -> list[dict]:
        run_or_404(id)
        return [_row(g) for g in kc.rows(m.GuardrailEvent, id, order_by=m.GuardrailEvent.created_at)]

    # ------------------------------------------------------------------ project-scoped reviews
    base = "/projects/{project_id}"

    @r.get(base + "/rules")
    def rules(project_id: str, run_id: str | None = None, status_: str | None = Query(None, alias="status"),
              limit: int | None = None, offset: int = 0, p: Principal = Depends(who)) -> list[dict]:
        return [_row(x) for x in kc.rows(m.Rule, **scope(project_id, run_id), status=status_,
                                         order_by=m.Rule.rule_id, limit=page(limit), offset=offset)]

    @r.get(base + "/differentials")
    def differentials(project_id: str, run_id: str | None = None, limit: int | None = None, offset: int = 0,
                      p: Principal = Depends(who)) -> list[dict]:
        return [_row(x) for x in kc.rows(m.Differential, **scope(project_id, run_id),
                                         order_by=m.Differential.severity_score.desc(), limit=page(limit), offset=offset)]

    @r.post(base + "/differentials/{id}/decision")
    def differential_decision(project_id: str, id: str, body: DecisionIn, p: Principal = Depends(who)) -> dict:
        return decide("differential", id, body, p, project_id)

    @r.get(base + "/mappings")
    def mappings(project_id: str, run_id: str | None = None, limit: int | None = None, offset: int = 0,
                 p: Principal = Depends(who)) -> list[dict]:
        return [_row(x) for x in kc.rows(m.Mapping, **scope(project_id, run_id), order_by=m.Mapping.mapping_id,
                                         limit=page(limit), offset=offset)]

    @r.post(base + "/mappings/{id}/approve")
    def mapping_approve(project_id: str, id: str, body: DecisionIn, p: Principal = Depends(who)) -> dict:
        return decide("mapping", id, body, p, project_id)

    @r.get(base + "/dq/cohorts")
    def cohorts(project_id: str, run_id: str | None = None, limit: int | None = None, offset: int = 0,
                p: Principal = Depends(who)) -> list[dict]:
        return [_row(x) for x in kc.rows(m.Cohort, **scope(project_id, run_id), order_by=m.Cohort.cohort_id,
                                         limit=page(limit), offset=offset)]

    @r.post(base + "/dq/cohorts/{id}/decision")
    def cohort_decision(project_id: str, id: str, body: DecisionIn, p: Principal = Depends(who)) -> dict:
        return decide("cohort", id, body, p, project_id)

    @r.get(base + "/dq/matches")
    def matches(project_id: str, run_id: str | None = None, limit: int | None = None, offset: int = 0,
                p: Principal = Depends(who)) -> list[dict]:
        return [_row(x) for x in kc.rows(m.EntityMatch, **scope(project_id, run_id), order_by=m.EntityMatch.match_id,
                                         limit=page(limit), offset=offset)]

    @r.post(base + "/dq/matches/{id}/decision")
    def match_decision(project_id: str, id: str, body: DecisionIn, p: Principal = Depends(who)) -> dict:
        return decide("entity_match", id, body, p, project_id)

    @r.get(base + "/dq/profiles")
    def profiles(project_id: str, run_id: str | None = None, p: Principal = Depends(who)) -> list[dict]:
        return [x.body | {"profile_id": x.profile_id, "run_id": x.run_id}
                for x in kc.rows(m.ColumnProfile, **scope(project_id, run_id), order_by=m.ColumnProfile.entity)]

    @r.get(base + "/dq/live-archive")
    def live_archive(project_id: str, run_id: str, recommendation: str | None = None, limit: int | None = None,
                     offset: int = 0, p: Principal = Depends(who)) -> dict:
        scope(project_id, run_id)
        return {"total": kc.count(m.LiveArchiveScore, run_id, recommendation=recommendation),
                "items": [_row(x) for x in kc.rows(m.LiveArchiveScore, run_id, recommendation=recommendation,
                                                   order_by=m.LiveArchiveScore.score_id, limit=page(limit),
                                                   offset=offset)]}

    @r.get(base + "/reconciliation/breaks")
    def breaks(project_id: str, run_id: str | None = None, cluster_id: str | None = None, limit: int | None = None,
               offset: int = 0, p: Principal = Depends(who)) -> list[dict]:
        return [_row(x, {"body"}) for x in kc.rows(m.ReconciliationBreak, **scope(project_id, run_id),
                                                   cluster_id=cluster_id, order_by=m.ReconciliationBreak.break_id,
                                                   limit=page(limit), offset=offset)]

    @r.get(base + "/reconciliation/clusters")
    def clusters(project_id: str, run_id: str | None = None, p: Principal = Depends(who)) -> list[dict]:
        return [_row(x) for x in kc.rows(m.BreakCluster, **scope(project_id, run_id),
                                         order_by=m.BreakCluster.count.desc())]

    @r.post(base + "/reconciliation/clusters/{id}/decision")
    def cluster_decision(project_id: str, id: str, body: DecisionIn, p: Principal = Depends(who)) -> dict:
        return decide("break_cluster", id, body, p, project_id)

    @r.get(base + "/conduct-cases")
    def conduct_cases(project_id: str, run_id: str | None = None, band: str | None = None, limit: int | None = None,
                      offset: int = 0, p: Principal = Depends(who)) -> list[dict]:
        return [_row(x) for x in kc.rows(m.ConductCase, **scope(project_id, run_id), band=band,
                                         order_by=m.ConductCase.priority, limit=page(limit), offset=offset)]

    @r.post(base + "/conduct-cases/{id}/decision")
    def conduct_decision(project_id: str, id: str, body: DecisionIn, p: Principal = Depends(who)) -> dict:
        return decide("conduct_case", id, body, p, project_id)

    @r.get(base + "/stories")
    def stories(project_id: str, run_id: str | None = None, p: Principal = Depends(who)) -> list[dict]:
        return [x.body | {"status": x.status} for x in kc.rows(m.Story, **scope(project_id, run_id),
                                                               order_by=m.Story.story_id)]

    @r.get(base + "/generated-code")
    def generated_code(project_id: str, run_id: str | None = None, p: Principal = Depends(who)) -> list[dict]:
        return [x.body | {"status": x.status} for x in kc.rows(m.GeneratedCode, **scope(project_id, run_id),
                                                               order_by=m.GeneratedCode.module_name)]

    @r.get(base + "/hitl/tasks")
    def hitl(project_id: str, run_id: str | None = None, status_: str | None = Query(None, alias="status"),
             gate: str | None = None, limit: int | None = None, offset: int = 0, p: Principal = Depends(who)) -> list[dict]:
        sc = scope(project_id, run_id)
        return [_row(t) for t in kc.list_tasks(sc.get("run_id"), status=status_, gate=gate,
                                               project_id=sc.get("project_id"), limit=page(limit), offset=offset)]

    @r.post(base + "/hitl/tasks/{id}/decision")
    def task_decision(project_id: str, id: str, body: DecisionIn, p: Principal = Depends(who)) -> dict:
        task = kc.get(m.HitlTask, id)
        if task is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
        run_or_404(task.run_id, project_id)
        return decide_task(task, body, p)

    # ------------------------------------------------------------------ assistant, readiness, audit, evidence
    @r.post(base + "/assistant/ask")
    def ask(project_id: str, body: QuestionIn, p: Principal = Depends(who)) -> dict:
        project_or_404(project_id)
        run_id = body.run_id
        if run_id:
            run_or_404(run_id, project_id)
        else:
            run_id = next((x.run_id for x in kc.list_runs(project_id) if x.status == "completed"), None)
        if run_id is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "this project has no certified knowledge yet")
        scoped = services.for_project(project_id)
        out = agent_class("assistant_agent")(scoped, run_id).execute(question=body.question)
        return out.items["answer"][0].model_dump(mode="json") | {"run_id": run_id}

    @r.get(base + "/readiness")
    def readiness(project_id: str, run_id: str | None = None, p: Principal = Depends(who)) -> dict:
        project_or_404(project_id)
        rid = run_id or next((x.run_id for x in kc.list_runs(project_id)), None)
        if rid is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no runs yet")
        run = run_or_404(rid, project_id)
        reports = kc.rows(m.AssuranceReport, rid, order_by=m.AssuranceReport.created_at.desc(), limit=1)
        rep = reports[0].body if reports else {}
        return {"run_id": rid, "readiness_score": rep.get("readiness_score", 0.0),
                "assurance_passed": rep.get("passed", False), "blockers": rep.get("blockers", []),
                "capabilities": rep.get("capabilities", []), "gates": (run.summary or {}).get("gates", {}),
                "go_no_go": (run.summary or {}).get("go_no_go", {})}

    @r.get(base + "/waterfall")
    def waterfall(project_id: str, run_id: str | None = None, p: Principal = Depends(who)) -> dict:
        """Every source record accounted for (extracted = excluded + rejected + missing + loaded; loaded =
        reconciled + with breaks), plus the live/archive threshold curve."""
        project_or_404(project_id)
        rid = run_id or next((x.run_id for x in kc.list_runs(project_id)), None)
        if rid is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no runs yet")
        run = run_or_404(rid, project_id)
        summary = run.summary or {}
        return {"run_id": rid, "waterfall": summary.get("waterfall", []), "live_archive": summary.get("live_archive"),
                "controls": summary.get("controls"), "change_data": summary.get("change_data", [])}

    @r.get(base + "/coverage")
    def coverage_matrix(project_id: str, run_id: str | None = None, p: Principal = Depends(who)) -> dict:
        """Product x rule-domain coverage of the rule base (``heuristics.coverage.product_field``)."""
        from services.coverage import coverage
        project_or_404(project_id)
        rid = run_id or next((x.run_id for x in kc.list_runs(project_id)), None)
        if rid is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no runs yet")
        run_or_404(rid, project_id)
        return coverage(kc, services.staging, rid, services.for_project(project_id).settings)

    @r.get(base + "/dependencies")
    def dependencies(project_id: str, run_id: str | None = None, max_edges: int = Query(1000, ge=1, le=5000),
                     p: Principal = Depends(who)) -> dict:
        project_or_404(project_id)
        rid = run_id or next((x.run_id for x in kc.list_runs(project_id)), None)
        if rid is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no runs yet")
        run_or_404(rid, project_id)
        return kc.dependency_graph(rid, max_edges=max_edges)

    @r.get(base + "/audit")
    def audit(project_id: str, run_id: str | None = None, entity_type: str | None = None,
              entity_id: str | None = None, limit: int | None = None, offset: int = 0,
              p: Principal = Depends(who)) -> list[dict]:
        sc = scope(project_id, run_id)
        return [_row(a) for a in kc.audit_for(entity_type, entity_id, sc.get("run_id"), limit=page(limit),
                                              project_id=sc.get("project_id") or project_id, offset=offset)]

    @r.get(base + "/evidence/{entity_type}/{entity_id}")
    def evidence(project_id: str, entity_type: str, entity_id: str, p: Principal = Depends(who)) -> list[dict]:
        project_or_404(project_id)
        allowed = set(kc.project_run_ids(project_id))
        return [_row(x) for x in kc.provenance_for(entity_type, entity_id) if x.run_id in allowed]

    @r.get("/evals/latest")
    def evals_latest(p: Principal = Depends(who)) -> list[dict]:
        latest: dict[str, Any] = {}
        for e in sorted(kc.rows(m.EvalResult), key=lambda x: x.created_at):
            latest[e.capability] = _row(e)
        return list(latest.values())

    app.include_router(r)

    @app.exception_handler(ControlPlaneViolation)
    async def _violation(_: Request, exc: ControlPlaneViolation):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(ScopeRequired)
    async def _scope(_: Request, exc: ScopeRequired):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(NotFound)
    async def _missing(_: Request, exc: NotFound):
        return JSONResponse(status_code=404, content={"detail": f"not found: {exc}"})

    @app.exception_handler(RunError)
    async def _run_error(_: Request, exc: RunError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    return app


def contract_paths() -> set[tuple[str, str]]:
    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    return {(path, method.upper()) for path, ops in spec["paths"].items() for method in ops}
