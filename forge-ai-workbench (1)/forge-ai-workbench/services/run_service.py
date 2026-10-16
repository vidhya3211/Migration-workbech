"""Run orchestration service: projects, chunked uploads, runs, durable human decisions, resume, kill-switch.

The API and CLI call this; nothing here writes to the knowledge core except through the repository. Long work
(validating a multi-GB bundle, running or resuming a graph) is a durable job executed by a worker - never
inside an API request - so a run waiting for a human holds no thread, and a worker crash loses nothing.

Every run is compiled from its project's pipeline *snapshot* (stored on the run), so a run parked at a gate
resumes on exactly the graph it started with even if the project's pipeline changes meanwhile.
"""
from __future__ import annotations

import math
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from langgraph.types import Command

from agents.base import AgentFailed
from config.settings import InvalidProjectSettings, effective_settings
from db import models as m
from graph.checkpointer import build_checkpointer
from graph.nodes import RunHalted
from graph.pipeline import GraphCache, PipelineError, WorkbenchContext, pipeline_hash, resolve, step_names
from ingestion.upload import UploadRejected, inspect_bundle, manifest, open_zip
from observability import context
from observability.logging.logger import Event, log_event
from observability.tracing.tracer import SpanKind, get_tracer
from schemas.audit import AuditEvent
from schemas.common import SystemRole, new_id, utc_now
from schemas.differential import HumanDecision
from schemas.execution import ExecutionStatus
from services.jobs import JobQueue, Worker
from services.runtime import Services
from storage.objects import iter_chunks


class RunError(ValueError):
    pass


class RunService:
    def __init__(self, services: Services, checkpointer=None) -> None:
        self.s = services
        self.checkpointer = checkpointer or build_checkpointer(services.settings)
        self.graphs = GraphCache(self.checkpointer)
        self.jobs = JobQueue(services.kc, services.settings.jobs)
        self.handlers = {"validate_upload": self._job_validate_upload, "start_run": self._job_start_run,
                         "resume_run": self._job_resume_run}
        self.worker = Worker(services.kc, services.settings.jobs, self.handlers, name=f"inline-{uuid.uuid4().hex[:6]}")
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # ================================================================== projects
    def create_project(self, *, name: str, business_domain: str, source_technologies: list[str],
                       target_technologies: list[str] | None, description: str, user: str,
                       settings_overrides: dict | None = None, pipeline: dict | None = None) -> m.Project:
        name = (name or "").strip()
        if not name:
            raise RunError("a project needs a name")
        domains = self.s.settings.branding.business_domains
        if business_domain not in domains:
            raise RunError(f"business domain must be one of {domains}")
        self._check_project_config(settings_overrides, pipeline)
        try:
            return self.s.kc.create_project(project_id=new_id("PRJ"), name=name, business_domain=business_domain,
                                            source_technologies=_clean(source_technologies),
                                            target_technologies=_clean(target_technologies or []),
                                            description=(description or "").strip(), created_by=user,
                                            settings_overrides=settings_overrides, pipeline=pipeline)
        except ValueError as exc:
            raise RunError(str(exc)) from exc

    def update_project(self, project_id: str, user: str, **fields) -> m.Project:
        fields = {k: v for k, v in fields.items() if v is not None}
        if "business_domain" in fields and fields["business_domain"] not in self.s.settings.branding.business_domains:
            raise RunError(f"business domain must be one of {self.s.settings.branding.business_domains}")
        for k in ("source_technologies", "target_technologies"):
            if k in fields:
                fields[k] = _clean(fields[k])
        if "status" in fields and fields["status"] not in ("active", "archived"):
            raise RunError("status must be active or archived")
        self._check_project_config(fields.get("settings_overrides"), fields.get("pipeline"))
        try:
            return self.s.kc.update_project(project_id, user, **fields)
        except KeyError as exc:
            raise RunError(f"project {project_id} not found") from exc
        except ValueError as exc:
            raise RunError(str(exc)) from exc

    def _check_project_config(self, overrides: dict | None, pipeline: dict | None) -> None:
        try:
            eff = effective_settings(self.s.platform_settings or self.s.settings, overrides) if overrides else None
            resolve(pipeline, (eff or self.s.settings).pipeline)
        except (InvalidProjectSettings, PipelineError) as exc:
            raise RunError(str(exc)) from exc

    def _project(self, project_id: str) -> m.Project:
        proj = self.s.kc.get_project(project_id)
        if proj is None:
            raise RunError(f"project {project_id} not found")
        if proj.status != "active":
            raise RunError(f"project {proj.name} is archived")
        return proj

    # ================================================================== uploads (chunked)
    def begin_upload(self, project_id: str, *, system_name: str, system_role: str, filename: str, size_bytes: int,
                     user: str) -> m.Upload:
        self._project(project_id)
        limits = self.s.settings.ingestion
        if not (system_name or "").strip():
            raise RunError("system_name must be supplied with every upload; it is never hardcoded")
        try:
            role = SystemRole(system_role).value
        except ValueError as exc:
            raise RunError("system_role must be 'source' or 'target'") from exc
        if size_bytes <= 0 or size_bytes > limits.max_zip_bytes:
            raise RunError(f"bundle size must be between 1 and {limits.max_zip_bytes} bytes")
        if not filename.lower().endswith(".zip"):
            raise RunError("upload a .zip bundle")
        chunk = limits.chunk_bytes
        return self.s.kc.begin_upload(upload_id=new_id("UPL"), project_id=project_id, system_name=system_name.strip(),
                                      system_role=role, filename=Path(filename).name, declared_bytes=size_bytes,
                                      chunk_bytes=chunk, chunks_expected=math.ceil(size_bytes / chunk), received_by=user)

    def _upload(self, upload_id: str, project_id: str | None = None) -> m.Upload:
        up = self.s.kc.get_upload(upload_id)
        if up is None or (project_id is not None and up.project_id != project_id):
            raise RunError(f"upload {upload_id} not found")
        return up

    def put_chunk(self, upload_id: str, index: int, data: bytes) -> dict:
        up = self._upload(upload_id)
        if up.status != "receiving":
            raise RunError(f"upload {upload_id} is {up.status}; chunks are no longer accepted")
        if not 0 <= index < up.chunks_expected:
            raise RunError(f"chunk index {index} outside 0..{up.chunks_expected - 1}")
        last = index == up.chunks_expected - 1
        expected = up.declared_bytes - up.chunk_bytes * (up.chunks_expected - 1) if last else up.chunk_bytes
        if len(data) != expected:
            raise RunError(f"chunk {index} is {len(data)} bytes; expected {expected}")
        self.s.objects.stage_part(self.s.settings.storage.uploads_container, f"{up.project_id}/{upload_id}.zip",
                                  index, data)
        return {"upload_id": upload_id, "index": index, "received": len(data)}

    def complete_upload(self, upload_id: str, user: str, *, background: bool = True) -> m.Upload:
        up = self._upload(upload_id)
        if up.status != "receiving":
            raise RunError(f"upload {upload_id} is already {up.status}")
        try:
            uri, size = self.s.objects.commit_parts(self.s.settings.storage.uploads_container,
                                                    f"{up.project_id}/{upload_id}.zip", up.chunks_expected)
        except ValueError as exc:
            raise RunError(str(exc)) from exc
        if size != up.declared_bytes:
            raise RunError(f"received {size} bytes but {up.declared_bytes} were declared")
        self.s.kc.update_upload(upload_id, status="validating", stored_uri=uri, bundle_bytes=size)
        job_id = self.jobs.enqueue("validate_upload", {"upload_id": upload_id}, created_by=user,
                                   project_id=up.project_id, upload_id=upload_id)
        if not background:
            self.worker.run_once(job_id)
        return self._upload(upload_id)

    def upload_stream(self, project_id: str, stream: BinaryIO, size_bytes: int, filename: str, system_name: str,
                      system_role: str, user: str, *, background: bool = False) -> m.Upload:
        """Server-side convenience (CLI, tests, feeds): the same chunked path the console uses."""
        up = self.begin_upload(project_id, system_name=system_name, system_role=system_role, filename=filename,
                               size_bytes=size_bytes, user=user)
        for i, buf in enumerate(iter_chunks(stream, up.chunk_bytes)):
            self.put_chunk(up.upload_id, i, buf)
        return self.complete_upload(up.upload_id, user, background=background)

    def upload_bytes(self, project_id: str, data: bytes, filename: str, system_name: str, system_role: str,
                     user: str) -> m.Upload:
        import io
        return self.upload_stream(project_id, io.BytesIO(data), len(data), filename, system_name, system_role, user)

    def _job_validate_upload(self, payload: dict) -> dict:
        upload_id = payload["upload_id"]
        up = self._upload(upload_id)
        limits = self.s.settings.ingestion
        try:
            with self.s.objects.open(up.stored_uri) as fh:
                import hashlib
                h = hashlib.sha256()
                for buf in iter(lambda: fh.read(limits.stream_buffer_bytes), b""):
                    h.update(buf)
                fh.seek(0)
                zf = open_zip(fh)
                members = inspect_bundle(zf, up.bundle_bytes, limits)
                entries = list((e.path, e.size_bytes, e.sha256) for e in manifest(zf, members, limits.stream_buffer_bytes))
        except UploadRejected as exc:
            self.s.kc.update_upload(upload_id, status="rejected", error=str(exc)[:2000])
            return {"status": "rejected", "error": str(exc)}
        n = self.s.kc.save_upload_files(upload_id, entries)
        by_ext: dict[str, int] = {}
        for p, _, _ in entries:
            ext = Path(p).suffix.lower() or "(none)"
            by_ext[ext] = by_ext.get(ext, 0) + 1
        self.s.kc.update_upload(upload_id, status="ready", bundle_sha256=h.hexdigest(), file_count=n,
                                uncompressed_bytes=sum(sz for _, sz, _ in entries),
                                manifest={"files": n, "by_extension": by_ext}, validated_at=utc_now(), error=None)
        return {"status": "ready", "files": n}

    # ================================================================== runs
    def start_run(self, project_id: str, upload_ids: list[str], user: str, *, background: bool = True) -> str:
        proj = self._project(project_id)
        uploads = [self.s.kc.get_upload(u) for u in upload_ids]
        if any(u is None or u.project_id != project_id for u in uploads):
            raise RunError("unknown upload id for this project")
        if any(u.status != "ready" for u in uploads):
            raise RunError("every upload must be validated (status 'ready') before a run starts")
        if any(u.run_id for u in uploads):
            raise RunError("an upload can only be used by one run")
        roles = {u.system_role for u in uploads}
        if "source" not in roles:
            raise RunError("a run needs at least one source upload")
        names = [(u.system_name.lower(), u.system_role) for u in uploads]
        if len(set(names)) != len(names):
            raise RunError("system_name must be unique within a run")
        scoped = self.s.for_project(project_id)
        try:
            snapshot = resolve(proj.pipeline, scoped.settings.pipeline)
        except PipelineError as exc:
            raise RunError(f"the project's pipeline is invalid: {exc}") from exc
        run_id = "RUN-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4].upper()
        src = ", ".join(u.system_name for u in uploads if u.system_role == "source")
        tgt = ", ".join(u.system_name for u in uploads if u.system_role == "target")
        self.s.kc.create_run(run_id, user, src, tgt or None, trace_id=uuid.uuid4().hex, project_id=project_id,
                             pipeline=snapshot, pipeline_hash=pipeline_hash(snapshot),
                             steps_total=len(step_names(snapshot)), settings_fingerprint=scoped.settings.fingerprint())
        for u in uploads:
            self.s.kc.update_upload(u.upload_id, status="consumed", run_id=run_id)
        job_id = self.jobs.enqueue("start_run", {"run_id": run_id, "upload_ids": upload_ids}, created_by=user,
                                   project_id=project_id, run_id=run_id)
        if not background:
            self.worker.run_once(job_id)
        return run_id

    def _lock(self, run_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(run_id, threading.Lock())

    def _graph(self, run: m.IngestRun):
        return self.graphs.get(run.pipeline)

    def _job_start_run(self, payload: dict) -> dict:
        run_id = payload["run_id"]
        run = self.s.kc.get_run(run_id)
        if run is None:
            raise RunError(f"run {run_id} not found")
        graph = self._graph(run)
        snap = graph.get_state({"configurable": {"thread_id": run_id}})
        if run.status in ("awaiting_human", "completed", "halted", "failed", "blocked", "budget_stopped"):
            return {"status": run.status, "note": "nothing to do"}
        if snap and snap.values:     # a worker died mid-run: continue from the last checkpoint
            self._invoke(run_id, None)
        else:
            self._invoke(run_id, {"run_id": run_id, "project_id": run.project_id, "upload_ids": payload["upload_ids"]})
        return {"status": self.s.kc.get_run(run_id).status}

    def _job_resume_run(self, payload: dict) -> dict:
        run_id = payload["run_id"]
        gate = self.current_gate(run_id)
        if gate is None:
            return {"status": "not_waiting"}
        decided = self.s.kc.list_tasks(run_id, status="decided", gate=gate)
        self._invoke(run_id, Command(resume={"decisions": {t.task_id: t.decision for t in decided}}))
        return {"status": self.s.kc.get_run(run_id).status}

    def _invoke(self, run_id: str, payload) -> None:
        run = self.s.kc.get_run(run_id)
        scoped = self.s.for_project(run.project_id)
        graph = self._graph(run)
        cfg = {"configurable": {"thread_id": run_id}, "recursion_limit": 500,
               "max_concurrency": scoped.settings.scale.fanout_max_concurrency}
        with self._lock(run_id), context.bind(run_id=run_id, trace_id=run.trace_id, project_id=run.project_id):
            try:
                with get_tracer().span(SpanKind.RUN, run_id):
                    log_event(Event.RUN_STARTED if not isinstance(payload, Command) else Event.RUN_INTERRUPTED,
                              "resuming" if isinstance(payload, Command) else "starting")
                    result = graph.invoke(payload, cfg, context=WorkbenchContext(scoped))
                if result.get("__interrupt__"):
                    log_event(Event.RUN_INTERRUPTED, "waiting for a named human", gate=self.current_gate(run_id))
                else:
                    log_event(Event.RUN_COMPLETED)
            except RunHalted as exc:
                self._end(run_id, "halted", str(exc))
            except AgentFailed as exc:
                status = "budget_stopped" if exc.status is ExecutionStatus.BUDGET_STOPPED else \
                    "blocked" if exc.status is ExecutionStatus.BLOCKED else "failed"
                self._end(run_id, status, str(exc))
            except Exception as exc:   # explicit failure state - never swallowed, never approval
                self._end(run_id, "failed", f"{type(exc).__name__}: {exc}")
            finally:
                self.s.kc.update_run(run_id, ledger=scoped.ledger(run_id).snapshot())
                scoped.release(run_id)

    def _end(self, run_id: str, status: str, reason: str) -> None:
        self.s.kc.update_run(run_id, status=status, halted_reason=reason[:2000], finished_at=datetime.now(timezone.utc))
        log_event(Event.RUN_FAILED, reason[:500], status=status)

    # ================================================================== human decisions
    def current_gate(self, run_id: str) -> str | None:
        run = self.s.kc.get_run(run_id)
        return run.current_gate if run else None

    def decide_task(self, task_id: str, decision: HumanDecision, *, auto_resume: bool = True,
                    background: bool = True) -> dict:
        task = self.s.kc.record_task_decision(task_id, decision)
        pending = self.s.kc.list_tasks(task.run_id, status="pending", gate=task.gate)
        resumed = False
        if auto_resume and not pending:
            self.resume(task.run_id, background=background)
            resumed = True
        return {"task_id": task_id, "gate": task.gate, "pending_in_gate": len(pending), "resumed": resumed}

    def decide_item(self, item_type: str, item_id: str, decision: HumanDecision, **kw) -> dict:
        task = self.s.kc.task_for_item(item_type, item_id)
        if task is None or task.status != "pending":
            raise RunError(f"no open review task for {item_type} {item_id}")
        return self.decide_task(task.task_id, decision.model_copy(update={"item_id": item_id,
                                                                          "item_type": item_type}), **kw)

    def resume(self, run_id: str, *, closed_by: str | None = None, background: bool = True) -> str:
        """Resume the durable interrupt with every recorded decision. Undecided items become unresolved."""
        gate = self.current_gate(run_id)
        if gate is None:
            raise RunError(f"run {run_id} is not waiting at a human gate")
        if closed_by:
            with self.s.kc.session() as sess, sess.begin():
                self.s.kc._audit(sess, AuditEvent(audit_id=new_id("AUD"), run_id=run_id, who=closed_by,
                                                  actor_type="human", what="close_gate", entity_type="gate",
                                                  entity_id=gate, after={"decided": len(self.s.kc.list_tasks(
                                                      run_id, status="decided", gate=gate))},
                                                  reason="gate closed with open items; they become unresolved"))
        run = self.s.kc.get_run(run_id)
        job_id = self.jobs.enqueue("resume_run", {"run_id": run_id}, created_by=closed_by or "system",
                                   project_id=run.project_id, run_id=run_id)
        if not background:
            self.worker.run_once(job_id)
        return job_id

    def halt(self, run_id: str, user: str) -> None:
        """Kill-switch: the run stops at the next node boundary."""
        self.s.kc.update_run(run_id, status="halt_requested", halted_reason=f"kill-switch by {user}")

    # ================================================================== reads
    def status(self, run_id: str) -> dict:
        run = self.s.kc.get_run(run_id)
        if run is None:
            raise RunError(f"run {run_id} not found")
        snap = self._graph(run).get_state({"configurable": {"thread_id": run_id}})
        done = list(run.steps_completed or [])
        return {
            "run_id": run.run_id, "project_id": run.project_id, "status": run.status, "current_gate": run.current_gate,
            "source_system": run.source_system, "target_system": run.target_system, "created_by": run.created_by,
            "created_at": run.created_at, "finished_at": run.finished_at, "halted_reason": run.halted_reason,
            "trace_id": run.trace_id, "next": list(snap.next) if snap else [], "summary": run.summary,
            "ledger": run.ledger, "pending_tasks": len(self.s.kc.list_tasks(run_id, status="pending")),
            "pipeline": {"name": run.pipeline_name, "hash": run.pipeline_hash, "phases": run.pipeline.get("phases", [])},
            "progress": {"steps_total": run.steps_total, "steps_completed": len(done),
                         "ratio": round(len(done) / run.steps_total, 4) if run.steps_total else 0.0,
                         "completed": done},
            "jobs": [{"job_id": j.job_id, "kind": j.kind, "status": j.status, "attempts": j.attempts,
                      "error": (j.error or "")[:500]} for j in self.s.kc.jobs_for(run_id=run_id)[:5]],
        }


def _clean(values: list[str] | None) -> list[str]:
    out, seen = [], set()
    for v in values or []:
        for part in str(v).split(","):
            p = part.strip()
            if p and p.lower() not in seen:
                seen.add(p.lower())
                out.append(p)
    return out


def sample_bundle(role: str) -> tuple[bytes, str]:
    from ingestion.upload import zip_directory
    root = Path(__file__).resolve().parent.parent / "samples" / role
    return zip_directory(root), f"{role}.zip"
