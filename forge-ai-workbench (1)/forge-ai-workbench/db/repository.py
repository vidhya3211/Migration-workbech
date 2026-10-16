"""Knowledge-core repository - the only write path into certified structured truth.

Non-negotiable: every write to rule, mapping, cohort, reconciliation_break and conduct_case carries matching
provenance rows and an audit row *in the same transaction*. There is no method that writes one without the
others. Certification only happens through ``apply_human_decision``, which requires a named human in the role
the control plane demands for that item type.

Scale: high-volume governed writes (breaks, conduct cases, entity matches) go through ``_governed_bulk`` -
batched ``INSERT .. ON CONFLICT`` with stable provenance ids, so a retried node never duplicates evidence.
Reads that can be large stream from server-side cursors. Every read is scoped to a run or a project.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Iterator, Sequence

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from blackboard.control_plane import (
    ControlPlaneViolation, transition, validate_agent_status, validate_decision_value, validate_human_decision,
    validate_write,
)
from blackboard.identity import get_identity
from db import models as m
from observability import context
from observability.logging.logger import Event, log_event
from schemas.artefact import Artefact, UploadManifest
from schemas.audit import AuditEvent
from schemas.build import AssuranceReport, HitlTask, UserStory
from schemas.common import HumanRole, ItemStatus, new_id, stable_id, utc_now
from schemas.differential import AdjudicationBrief, Differential, HumanDecision
from schemas.dq import Cohort, EntityMatch
from schemas.execution import AgentExecution, GuardrailResult, HeadroomMetrics, LLMCallRecord
from schemas.mapping import DictionaryEntry, GeneratedCode, MappingProposal
from schemas.provenance import Evidence
from schemas.recon import BreakCluster, ConductCase, ConductOutcome, ReconciliationBreak
from schemas.rules import CandidateRule
from schemas.signals import Signal


class MissingProvenance(ControlPlaneViolation):
    """A governed write arrived without evidence. Refused - no certification without evidence."""


class NotFound(KeyError):
    pass


class ScopeRequired(ValueError):
    """A read that could span projects was attempted without a run or project scope."""


# entity type -> (ORM class, primary-key attribute)
ENTITY_TABLES: dict[str, tuple[type, str]] = {
    "rule": (m.Rule, "rule_id"),
    "differential": (m.Differential, "differential_id"),
    "mapping": (m.Mapping, "mapping_id"),
    "cohort": (m.Cohort, "cohort_id"),
    "entity_match": (m.EntityMatch, "match_id"),
    "reconciliation_break": (m.ReconciliationBreak, "break_id"),
    "break_cluster": (m.BreakCluster, "cluster_id"),
    "conduct_case": (m.ConductCase, "case_id"),
    "story": (m.Story, "story_id"),
    "generated_code": (m.GeneratedCode, "code_id"),
}


def _dump(obj: Any) -> Any:
    return obj.model_dump(mode="json") if hasattr(obj, "model_dump") else obj


class KnowledgeCore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._sf = session_factory

    def session(self) -> Session:
        return self._sf()

    # ================================================================== audit + provenance primitives
    def _audit(self, s: Session, event: AuditEvent) -> str:
        s.add(m.Audit(
            audit_id=event.audit_id, run_id=event.run_id, who=event.who, actor_type=event.actor_type, what=event.what,
            entity_type=event.entity_type, entity_id=event.entity_id, when=event.when, before=event.before,
            after=event.after, agent=event.agent, model=event.model, prompt_version=event.prompt_version,
            tool_version=event.tool_version, reason=event.reason, trace_id=event.trace_id or context.get("trace_id"),
        ))
        log_event(Event.AUDIT_WRITTEN, entity_type=event.entity_type, entity_id=event.entity_id, what=event.what)
        return event.audit_id

    def _provenance(self, s: Session, run_id: str, entity_type: str, entity_id: str,
                    evidence: Sequence[Evidence]) -> list[str]:
        ids = []
        for i, ev in enumerate(evidence):
            pid = stable_id("PRV", entity_type, entity_id, i, ev.artefact_id, ev.locator.model_dump_json())
            s.merge(m.Provenance(
                provenance_id=pid, run_id=run_id, entity_type=entity_type, entity_id=entity_id,
                artefact_id=ev.artefact_id, locator=ev.locator.model_dump(mode="json"), excerpt=ev.excerpt,
                source_sha256=ev.source_sha256, system_name=ev.system.system_name,
                system_role=ev.system.system_role.value, created_at=utc_now(),
            ))
            ids.append(pid)
        if ids:
            log_event(Event.PROVENANCE_WRITTEN, entity_type=entity_type, entity_id=entity_id, count=len(ids))
        return ids

    def _governed_write(self, s: Session, *, run_id: str, agent: str, entity_type: str, entity_id: str,
                        row: Any, evidence: Sequence[Evidence], status: ItemStatus, summary: dict,
                        reason: str, model: str | None = None, prompt_version: str | None = None,
                        tool_version: str | None = None, what: str = "propose") -> None:
        validate_write(agent, entity_type)
        validate_agent_status(agent, status)
        if not evidence:
            raise MissingProvenance(f"{agent} tried to write {entity_type} {entity_id} without evidence")
        s.merge(row)
        self._provenance(s, run_id, entity_type, entity_id, evidence)
        self._audit(s, AuditEvent(
            audit_id=new_id("AUD"), run_id=run_id, who=agent, actor_type="agent", what=what,
            entity_type=entity_type, entity_id=entity_id, after=summary, agent=agent, model=model,
            prompt_version=prompt_version, tool_version=tool_version, reason=reason,
        ))
        log_event(Event.KNOWLEDGE_CORE_WRITE, entity_type=entity_type, entity_id=entity_id, status=status.value)

    def _governed_bulk(self, *, run_id: str, agent: str, entity_type: str, items: list[tuple[dict, Sequence[Evidence],
                       ItemStatus, dict, str]], reason_what: str = "propose") -> int:
        """Batched governed write: rows upserted, provenance (stable ids) and audit rows in one transaction."""
        if not items:
            return 0
        validate_write(agent, entity_type)
        for status in {it[2] for it in items}:
            validate_agent_status(agent, status)
        cls, pk = ENTITY_TABLES[entity_type]
        rows, prov, audits = [], [], []
        now = utc_now()
        trace = context.get("trace_id")
        for row, evidence, status, summary, reason in items:
            if not evidence:
                raise MissingProvenance(f"{agent} tried to write {entity_type} {row[pk]} without evidence")
            rows.append(row)
            for i, ev in enumerate(evidence):
                prov.append(dict(provenance_id=stable_id("PRV", entity_type, row[pk], i, ev.artefact_id,
                                                         ev.locator.model_dump_json()),
                                 run_id=run_id, entity_type=entity_type, entity_id=row[pk], artefact_id=ev.artefact_id,
                                 locator=ev.locator.model_dump(mode="json"), excerpt=ev.excerpt,
                                 source_sha256=ev.source_sha256, system_name=ev.system.system_name,
                                 system_role=ev.system.system_role.value, created_at=now))
            audits.append(dict(audit_id=new_id("AUD"), run_id=run_id, who=agent, actor_type="agent", what=reason_what,
                               entity_type=entity_type, entity_id=row[pk], when=now, before=None, after=summary,
                               agent=agent, model=None, prompt_version=None, tool_version=None, reason=reason[:2000],
                               trace_id=trace))
        table = cls.__table__
        with self._sf.begin() as s:
            ins = pg_insert(table)
            cols = [c.name for c in table.columns if c.name != pk and c.name in rows[0]]
            s.execute(ins.on_conflict_do_update(index_elements=[pk], set_={c: ins.excluded[c] for c in cols}), rows)
            for i in range(0, len(prov), 5000):
                s.execute(pg_insert(m.Provenance.__table__).on_conflict_do_nothing(index_elements=["provenance_id"]),
                          prov[i:i + 5000])
            s.execute(m.Audit.__table__.insert(), audits)
        log_event(Event.KNOWLEDGE_CORE_WRITE, entity_type=entity_type, count=len(rows), bulk=True)
        return len(rows)

    # ================================================================== projects
    def create_project(self, *, project_id: str, name: str, business_domain: str, source_technologies: list[str],
                       target_technologies: list[str], description: str, created_by: str,
                       settings_overrides: dict | None = None, pipeline: dict | None = None) -> m.Project:
        now = utc_now()
        with self._sf.begin() as s:
            if s.scalar(select(func.count()).select_from(m.Project).where(func.lower(m.Project.name) == name.lower())):
                raise ValueError(f"a project named {name!r} already exists")
            proj = m.Project(project_id=project_id, name=name, business_domain=business_domain,
                             source_technologies=source_technologies, target_technologies=target_technologies,
                             description=description, status="active", settings_overrides=settings_overrides or {},
                             pipeline=pipeline or {}, created_by=created_by, created_at=now, updated_at=now)
            s.add(proj)
            self._audit(s, AuditEvent(audit_id=new_id("AUD"), run_id="-", who=created_by, actor_type="human",
                                      what="create", entity_type="project", entity_id=project_id,
                                      after={"name": name, "business_domain": business_domain,
                                             "source_technologies": source_technologies,
                                             "target_technologies": target_technologies},
                                      reason="project created"))
        return proj

    def update_project(self, project_id: str, who: str, **fields: Any) -> m.Project:
        with self._sf.begin() as s:
            proj = s.get(m.Project, project_id)
            if proj is None:
                raise NotFound(project_id)
            if "name" in fields and fields["name"].lower() != proj.name.lower() and s.scalar(
                    select(func.count()).select_from(m.Project).where(func.lower(m.Project.name) == fields["name"].lower())):
                raise ValueError(f"a project named {fields['name']!r} already exists")
            before = {k: getattr(proj, k) for k in fields}
            for k, v in fields.items():
                setattr(proj, k, v)
            proj.updated_at = utc_now()
            self._audit(s, AuditEvent(audit_id=new_id("AUD"), run_id="-", who=who, actor_type="human", what="update",
                                      entity_type="project", entity_id=project_id,
                                      before=json.loads(json.dumps(before, default=str)),
                                      after=json.loads(json.dumps(fields, default=str)), reason="project updated"))
            return proj

    def get_project(self, project_id: str) -> m.Project | None:
        with self._sf() as s:
            return s.get(m.Project, project_id)

    def list_projects(self, include_archived: bool = False) -> list[m.Project]:
        q = select(m.Project).order_by(m.Project.created_at.desc())
        if not include_archived:
            q = q.where(m.Project.status == "active")
        with self._sf() as s:
            return list(s.scalars(q))

    def project_run_ids(self, project_id: str) -> list[str]:
        with self._sf() as s:
            return list(s.scalars(select(m.IngestRun.run_id).where(m.IngestRun.project_id == project_id)))

    # ================================================================== runs, uploads, artefacts
    def create_run(self, run_id: str, created_by: str, source_system: str | None, target_system: str | None,
                   trace_id: str | None = None, *, project_id: str, pipeline: dict, pipeline_hash: str,
                   steps_total: int, settings_fingerprint: str = "") -> None:
        with self._sf.begin() as s:
            s.add(m.IngestRun(run_id=run_id, project_id=project_id, status="created", created_by=created_by,
                              created_at=utc_now(), source_system=source_system, target_system=target_system,
                              summary={}, ledger={}, trace_id=trace_id, pipeline_name=pipeline.get("name", "default"),
                              pipeline=pipeline, pipeline_hash=pipeline_hash, steps_total=steps_total,
                              steps_completed=[], settings_fingerprint=settings_fingerprint))
            self._audit(s, AuditEvent(audit_id=new_id("AUD"), run_id=run_id, who=created_by, actor_type="human",
                                      what="create", entity_type="ingest_run", entity_id=run_id,
                                      after={"project_id": project_id, "source_system": source_system,
                                             "target_system": target_system, "pipeline": pipeline.get("name"),
                                             "pipeline_hash": pipeline_hash},
                                      reason="run requested"))

    def mark_step(self, run_id: str, step: str) -> None:
        with self._sf.begin() as s:
            s.execute(text("UPDATE ingest_run SET steps_completed = steps_completed || to_jsonb(CAST(:st AS text)) "
                           "WHERE run_id = :r AND NOT (steps_completed ? :st)"), {"r": run_id, "st": step})

    def merge_run_summary(self, run_id: str, patch: dict) -> None:
        """Add keys to the run summary without replacing it (steps publish results as they finish)."""
        with self._sf.begin() as s:
            s.execute(text("UPDATE ingest_run SET summary = coalesce(summary, '{}'::jsonb) || CAST(:p AS jsonb) "
                           "WHERE run_id = :r"), {"p": json.dumps(patch, default=str), "r": run_id})

    def update_run(self, run_id: str, **fields: Any) -> None:
        with self._sf.begin() as s:
            run = s.get(m.IngestRun, run_id)
            if run is None:
                raise KeyError(run_id)
            before = {k: getattr(run, k) for k in fields if k in ("status", "current_gate", "halted_reason")}
            for key, value in fields.items():
                setattr(run, key, value)
            if "status" in fields:
                self._audit(s, AuditEvent(audit_id=new_id("AUD"), run_id=run_id, who="orchestrator_agent",
                                          actor_type="system", what="status", entity_type="ingest_run",
                                          entity_id=run_id, before={k: str(v) for k, v in before.items()},
                                          after={"status": fields["status"], "current_gate": fields.get("current_gate")},
                                          reason=fields.get("halted_reason") or ""))

    def get_run(self, run_id: str) -> m.IngestRun | None:
        with self._sf() as s:
            return s.get(m.IngestRun, run_id)

    def list_runs(self, project_id: str, limit: int = 50, offset: int = 0) -> list[m.IngestRun]:
        with self._sf() as s:
            return list(s.scalars(select(m.IngestRun).where(m.IngestRun.project_id == project_id)
                                  .order_by(m.IngestRun.created_at.desc()).limit(limit).offset(offset)))

    def latest_runs(self) -> dict[str, m.IngestRun]:
        """project_id -> its most recent run (portfolio view)."""
        with self._sf() as s:
            rows = s.scalars(select(m.IngestRun).distinct(m.IngestRun.project_id)
                             .order_by(m.IngestRun.project_id, m.IngestRun.created_at.desc()))
            return {r.project_id: r for r in rows}

    def begin_upload(self, *, upload_id: str, project_id: str, system_name: str, system_role: str, filename: str,
                     declared_bytes: int, chunk_bytes: int, chunks_expected: int, received_by: str) -> m.Upload:
        with self._sf.begin() as s:
            row = m.Upload(upload_id=upload_id, project_id=project_id, run_id=None, system_name=system_name,
                           system_role=system_role, filename=filename, status="receiving", declared_bytes=declared_bytes,
                           chunk_bytes=chunk_bytes, chunks_expected=chunks_expected, received_by=received_by,
                           received_at=utc_now(), manifest={})
            s.add(row)
            self._audit(s, AuditEvent(audit_id=new_id("AUD"), run_id="-", who=received_by, actor_type="human",
                                      what="upload_started", entity_type="upload", entity_id=upload_id,
                                      after={"project_id": project_id, "system_name": system_name,
                                             "system_role": system_role, "declared_bytes": declared_bytes},
                                      reason="console upload"))
            return row

    def update_upload(self, upload_id: str, **fields: Any) -> None:
        with self._sf.begin() as s:
            row = s.get(m.Upload, upload_id)
            if row is None:
                raise NotFound(upload_id)
            for k, v in fields.items():
                setattr(row, k, v)
            if "status" in fields:
                self._audit(s, AuditEvent(audit_id=new_id("AUD"), run_id=row.run_id or "-", who="ingest_agent",
                                          actor_type="system", what=f"upload_{fields['status']}", entity_type="upload",
                                          entity_id=upload_id, after={"status": fields["status"],
                                                                      "error": fields.get("error")},
                                          reason=fields.get("error") or ""))

    def save_upload_files(self, upload_id: str, entries: Iterable[tuple[str, int, str]], batch: int = 5000) -> int:
        n = 0
        with self._sf.begin() as s:
            s.execute(delete(m.UploadFile).where(m.UploadFile.upload_id == upload_id))
            buf: list[dict] = []
            for path, size, sha in entries:
                buf.append({"upload_id": upload_id, "path": path, "size_bytes": size, "sha256": sha})
                if len(buf) >= batch:
                    s.execute(m.UploadFile.__table__.insert(), buf)
                    n += len(buf)
                    buf = []
            if buf:
                s.execute(m.UploadFile.__table__.insert(), buf)
                n += len(buf)
        return n

    def upload_files(self, upload_id: str) -> dict[str, tuple[int, str]]:
        with self._sf() as s:
            return {p: (int(sz), sha) for p, sz, sha in s.execute(
                select(m.UploadFile.path, m.UploadFile.size_bytes, m.UploadFile.sha256)
                .where(m.UploadFile.upload_id == upload_id))}

    def list_uploads(self, project_id: str) -> list[m.Upload]:
        with self._sf() as s:
            return list(s.scalars(select(m.Upload).where(m.Upload.project_id == project_id)
                                  .order_by(m.Upload.received_at.desc())))

    def get_upload(self, upload_id: str) -> m.Upload | None:
        with self._sf() as s:
            return s.get(m.Upload, upload_id)

    def attach_upload(self, upload_id: str, run_id: str) -> None:
        with self._sf.begin() as s:
            s.execute(update(m.Upload).where(m.Upload.upload_id == upload_id).values(run_id=run_id))

    def save_artefacts(self, agent: str, artefacts: Iterable[Artefact]) -> int:
        n = 0
        with self._sf.begin() as s:
            for a in artefacts:
                validate_write(agent, "artefact")
                s.merge(m.Artefact(
                    artefact_id=a.artefact_id, run_id=a.run_id, upload_id=a.upload_id,
                    system_name=a.system.system_name, system_role=a.system.system_role.value, path=a.path,
                    artefact_type=a.artefact_type, language=a.language, encoding=a.encoding, sha256=a.sha256,
                    size_bytes=a.size_bytes, line_count=a.line_count, confidence=a.confidence, reasons=a.reasons,
                    route=a.route, blob_uri=a.blob_uri, classification=a.classification, ingest=a.ingest,
                ))
                self._audit(s, AuditEvent(audit_id=new_id("AUD"), run_id=a.run_id, who=agent, actor_type="agent",
                                          what="classify", entity_type="artefact", entity_id=a.artefact_id,
                                          after={"path": a.path, "type": a.artefact_type, "route": a.route,
                                                 "confidence": a.confidence}, agent=agent, reason="; ".join(a.reasons)))
                n += 1
        return n

    def set_artefact_analysis(self, artefact_id: str, status: str, summary: dict) -> None:
        with self._sf.begin() as s:
            s.execute(update(m.Artefact).where(m.Artefact.artefact_id == artefact_id)
                      .values(analysis_status=status, analysis=summary))

    def list_artefacts(self, run_id: str) -> list[m.Artefact]:
        with self._sf() as s:
            return list(s.scalars(select(m.Artefact).where(m.Artefact.run_id == run_id).order_by(m.Artefact.path)))

    # ================================================================== governed discovery writes
    def save_rules(self, rules: Iterable[CandidateRule], domains: dict[str, str] | None = None) -> int:
        n = 0
        with self._sf.begin() as s:
            for r in rules:
                status = ItemStatus.PROPOSED
                row = m.Rule(
                    rule_id=r.rule_id, run_id=r.run_id, artefact_id=r.artefact_id, agent=r.agent, tier=r.tier.value,
                    kind=r.kind.value, name=r.name, canonical_name=r.canonical_name,
                    domain=(domains or {}).get(r.rule_id), expression=r.expression, conditions=r.conditions,
                    inputs=r.inputs, confidence=r.confidence, confidence_reasons=r.confidence_reasons, notes=r.notes,
                    context=r.context, system_name=r.system.system_name, system_role=r.system.system_role.value,
                    language=r.language, status=status.value, requires_human=True,
                    reasoning_mode=r.reasoning_mode.value, created_at=utc_now(),
                )
                self._governed_write(s, run_id=r.run_id, agent=r.agent, entity_type="rule", entity_id=r.rule_id,
                                     row=row, evidence=[r.evidence], status=status,
                                     summary={"kind": r.kind.value, "name": r.name, "expression": r.expression,
                                              "conditions": r.conditions, "confidence": r.confidence},
                                     reason="; ".join(r.confidence_reasons), model=r.model,
                                     prompt_version=r.prompt_version)
                n += 1
        return n

    def save_differentials(self, diffs: Iterable[tuple[Differential, AdjudicationBrief | None]]) -> int:
        n = 0
        with self._sf.begin() as s:
            for d, brief in diffs:
                row = m.Differential(
                    differential_id=d.differential_id, run_id=d.run_id, canonical_name=d.canonical_name,
                    domain=d.domain, difference_type=d.difference_type.value, severity_score=d.severity.score,
                    financial_exposure=d.financial_exposure, status=d.status.value,
                    required_role=d.required_role.value, body=d.model_dump(mode="json"),
                    brief=brief.model_dump(mode="json") if brief else {}, created_at=utc_now(),
                )
                self._governed_write(s, run_id=d.run_id, agent="differential_agent", entity_type="differential",
                                     entity_id=d.differential_id, row=row, evidence=d.evidence, status=d.status,
                                     summary={"type": d.difference_type.value, "canonical_name": d.canonical_name,
                                              "severity": d.severity.score},
                                     reason=d.explanation[:500])
                n += 1
        return n

    def set_differential_briefs(self, run_id: str, briefs: Iterable[AdjudicationBrief]) -> int:
        """The Adjudication agent (T1) attaches its brief; it cannot change the differential itself."""
        n = 0
        with self._sf.begin() as s:
            for b in briefs:
                row = s.get(m.Differential, b.differential_id)
                if row is None:
                    continue
                row.brief = b.model_dump(mode="json")
                self._audit(s, AuditEvent(audit_id=new_id("AUD"), run_id=run_id, who="adjudication_agent",
                                          actor_type="agent", what="brief", entity_type="differential",
                                          entity_id=b.differential_id, after={"question": b.question,
                                                                              "role": b.required_role.value},
                                          agent="adjudication_agent", reason="evidence brief for the named expert"))
                n += 1
        return n

    def save_mappings(self, proposals: Iterable[MappingProposal]) -> int:
        n = 0
        with self._sf.begin() as s:
            for p in proposals:
                row = m.Mapping(
                    mapping_id=p.mapping_id, run_id=p.run_id, source_system=p.source.system.system_name,
                    source_entity=p.source.entity, source_field=p.source.field,
                    target_system=p.target.system.system_name, target_entity=p.target.entity,
                    target_field=p.target.field, transformation=p.transformation.model_dump(mode="json"),
                    score=p.score, reasoning_mode=p.reasoning_mode.value, status=p.status.value,
                    body=p.model_dump(mode="json"), validation={}, created_at=utc_now(),
                )
                self._governed_write(s, run_id=p.run_id, agent="mapping_agent", entity_type="mapping",
                                     entity_id=p.mapping_id, row=row, evidence=[p.source.evidence, p.target.evidence],
                                     status=p.status,
                                     summary={"source": f"{p.source.entity}.{p.source.field}",
                                              "target": f"{p.target.entity}.{p.target.field}", "score": p.score},
                                     reason="; ".join(p.reasoning_notes)[:500])
                n += 1
        return n

    def set_mapping_validation(self, mapping_id: str, validation: dict) -> None:
        with self._sf.begin() as s:
            s.execute(update(m.Mapping).where(m.Mapping.mapping_id == mapping_id).values(validation=validation))
            row = s.get(m.Mapping, mapping_id)
            self._audit(s, AuditEvent(audit_id=new_id("AUD"), run_id=row.run_id if row else "-",
                                      who="validation_agent", actor_type="agent", what="validate",
                                      entity_type="mapping", entity_id=mapping_id, after=validation,
                                      agent="validation_agent", reason=validation.get("verdict", "")))

    def save_dictionary(self, run_id: str, entries: Iterable[DictionaryEntry]) -> int:
        n = 0
        with self._sf.begin() as s:
            for e in entries:
                s.merge(m.DataDictionaryEntry(entry_id=e.entry_id, run_id=run_id, system_name=e.system.system_name,
                                              system_role=e.system.system_role.value, entity=e.entity, field=e.field,
                                              body=e.model_dump(mode="json")))
                n += 1
        return n

    def save_cohorts(self, cohorts: Iterable[Cohort]) -> int:
        n = 0
        with self._sf.begin() as s:
            for c in cohorts:
                row = m.Cohort(cohort_id=c.cohort_id, run_id=c.run_id, entity=c.entity, column=c.column,
                               defect_code=c.defect_code, record_count=c.record_count, status=c.status.value,
                               body=c.model_dump(mode="json"), root_cause={}, cleansing={}, created_at=utc_now())
                self._governed_write(s, run_id=c.run_id, agent="anomaly_agent", entity_type="cohort",
                                     entity_id=c.cohort_id, row=row, evidence=c.evidence, status=c.status,
                                     summary={"defect": c.defect_code, "column": c.column, "records": c.record_count},
                                     reason=c.description)
                n += 1
        return n

    def annotate_cohort(self, cohort_id: str, agent: str, *, root_cause: dict | None = None,
                        cleansing: dict | None = None) -> None:
        with self._sf.begin() as s:
            row = s.get(m.Cohort, cohort_id)
            if row is None:
                raise KeyError(cohort_id)
            if root_cause is not None:
                row.root_cause = root_cause
            if cleansing is not None:
                row.cleansing = cleansing
            self._audit(s, AuditEvent(audit_id=new_id("AUD"), run_id=row.run_id, who=agent, actor_type="agent",
                                      what="annotate", entity_type="cohort", entity_id=cohort_id,
                                      after={"root_cause": root_cause, "cleansing": cleansing}, agent=agent,
                                      reason="recommendation only; requires data steward review"))

    def save_entity_matches(self, matches: Iterable[EntityMatch], agent: str = "entity_match_agent") -> int:
        items = [(dict(match_id=x.match_id, run_id=x.run_id, left_ref=x.left_ref, right_ref=x.right_ref,
                       score=x.score.score, status=x.status.value, body=x.model_dump(mode="json"), decided_by=None,
                       created_at=utc_now()), x.evidence, x.status, {"score": x.score.score}, x.score.reason)
                 for x in matches]
        return self._governed_bulk(run_id=items[0][0]["run_id"] if items else "-", agent=agent,
                                   entity_type="entity_match", items=items)

    def save_breaks(self, breaks: Iterable[ReconciliationBreak], agent: str = "reconciler_agent") -> int:
        now = utc_now()
        items = [(dict(break_id=b.break_id, run_id=b.run_id, record_key=b.record_key, entity=b.entity, field=b.field,
                       break_type=b.break_type, source_value=b.source_value, expected_value=b.expected_value,
                       target_value=b.target_value, delta=b.delta, mapping_id=b.mapping_id, cluster_id=b.cluster_id,
                       status=b.status.value, lineage={}, body=b.model_dump(mode="json"), decided_by=None,
                       created_at=now),
                  b.evidence, b.status, {"field": b.field, "type": b.break_type, "delta": b.delta},
                  "deterministic comparison") for b in breaks]
        return self._governed_bulk(run_id=items[0][0]["run_id"] if items else "-", agent=agent,
                                   entity_type="reconciliation_break", items=items)

    _NUM = "'^-?[0-9]+(\.[0-9]+)?$'"
    _REL = ("(CASE WHEN delta ~ {n} AND expected_value ~ {n} AND expected_value::numeric <> 0 "
            "THEN delta::numeric / abs(expected_value::numeric) END)")
    _BAND = ("CASE WHEN {rel} IS NULL THEN 'non-numeric' WHEN {rel} > :band THEN 'up-material' "
             "WHEN {rel} < -:band THEN 'down-material' ELSE 'small' END")

    def break_groups(self, run_id: str, cfg: dict | None = None) -> list[dict]:
        """Break signatures for clustering, in three shapes:

        * ``exact`` - one (entity, field, type, delta): a uniform break that replay can attribute exactly;
        * ``shift`` - a *systematic* relative shift: many different deltas on one field and sign whose size is a
          consistent fraction of the expected value (cohort >= ``min_cohort``, dispersion of the relative shift
          <= ``max_dispersion``) - one cause, not hundreds of signatures;
        * ``band`` - when a field still fans out into more than ``max_signatures_per_field`` exact signatures,
          they collapse into relative bands (up / down beyond ``material_band``, small) so a person reads groups,
          not a worklist.
        """
        cfg = cfg or {}
        min_cohort = int(cfg.get("min_cohort", 25))
        max_disp = float(cfg.get("max_dispersion", 0.35))
        max_sigs = int(cfg.get("max_signatures_per_field", 50))
        band = float(cfg.get("material_band", 0.02))
        num, rel = self._NUM, self._REL.format(n=self._NUM)
        with self._sf() as s:
            rows = s.execute(text(
                "SELECT entity, field, break_type, delta, count(*) AS n, "
                f"coalesce(sum(CASE WHEN delta ~ {num} THEN delta::numeric END), 0) AS total, "
                "min(break_id) AS first_id FROM reconciliation_break WHERE run_id = :r "
                "GROUP BY entity, field, break_type, delta ORDER BY entity, field, break_type, delta NULLS FIRST"),
                {"r": run_id}).all()
            fams = s.execute(text(
                f"WITH b AS (SELECT entity, field, break_type, sign(delta::numeric) AS sg, {rel} AS rel, delta "
                f"FROM reconciliation_break WHERE run_id = :r AND delta ~ {num}), "
                "m AS (SELECT entity, field, break_type, sg, count(*) AS n, count(DISTINCT delta) AS nd, "
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY rel) AS med FROM b WHERE rel IS NOT NULL "
                "GROUP BY 1, 2, 3, 4) "
                "SELECT m.entity, m.field, m.break_type, m.sg, m.n, m.nd, m.med, "
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(b.rel - m.med)) AS mad "
                "FROM m JOIN b USING (entity, field, break_type, sg) WHERE b.rel IS NOT NULL "
                "GROUP BY m.entity, m.field, m.break_type, m.sg, m.n, m.nd, m.med"), {"r": run_id}).all()
        exact = [{"kind": "exact", "entity": e, "field": f, "break_type": bt, "delta": d, "count": int(n),
                  "total_delta": Decimal(tot), "first_break_id": fid} for e, f, bt, d, n, tot, fid in rows]
        shifts = {}
        for e, f, bt, sg, n, nd, med, mad in fams:
            disp = (float(mad) / abs(float(med))) if med else None
            if n >= min_cohort and nd > 1 and disp is not None and disp <= max_disp:
                shifts[(e, f, bt, int(sg))] = {"median_shift": round(float(med), 6), "dispersion": round(disp, 4),
                                               "cohort": int(n), "distinct_deltas": int(nd)}

        def sign_of(d: str | None) -> int | None:
            try:
                v = Decimal(d) if d is not None else None
            except Exception:
                return None
            return None if v is None else (1 if v > 0 else -1 if v < 0 else 0)
        by_field: dict[tuple, list[dict]] = defaultdict(list)
        for g in exact:
            fam = (g["entity"], g["field"], g["break_type"], sign_of(g["delta"]))
            if fam in shifts:
                by_field[("shift", *fam)].append(g)
            else:
                by_field[("field", g["entity"], g["field"], g["break_type"])].append(g)
        out: list[dict] = []
        for key, gs in by_field.items():
            if key[0] == "shift":
                _, e, f, bt, sg = key
                first = min(gs, key=lambda g: g["first_break_id"])
                out.append({"kind": "shift", "entity": e, "field": f, "break_type": bt, "delta": None, "sign": sg,
                            "count": sum(g["count"] for g in gs), "total_delta": sum(g["total_delta"] for g in gs),
                            "first_break_id": first["first_break_id"],
                            "shape": {"kind": "shift", "systematic": True, **shifts[(e, f, bt, sg)]}})
            elif len(gs) > max_sigs and any(g["delta"] is not None for g in gs):
                _, e, f, bt = key
                out.extend(self._band_groups(run_id, e, f, bt, band))
                out.extend(g for g in gs if g["delta"] is None)
            else:
                out.extend(gs)
        return sorted(out, key=lambda g: (g["entity"], g["field"], g["break_type"], g["kind"], str(g.get("delta"))))

    def _band_groups(self, run_id: str, entity: str, field: str, break_type: str, band: float) -> list[dict]:
        num, rel = self._NUM, self._REL.format(n=self._NUM)
        expr = self._BAND.format(rel=rel)
        with self._sf() as s:
            rows = s.execute(text(
                f"SELECT {expr} AS b, count(*), coalesce(sum(CASE WHEN delta ~ {num} THEN delta::numeric END), 0), "
                f"min(break_id), percentile_cont(0.5) WITHIN GROUP (ORDER BY {rel}) "
                "FROM reconciliation_break WHERE run_id = :r AND entity = :e AND field = :f AND break_type = :t "
                "AND delta IS NOT NULL GROUP BY 1"),
                {"r": run_id, "e": entity, "f": field, "t": break_type, "band": band}).all()
        return [{"kind": "band", "entity": entity, "field": field, "break_type": break_type, "delta": None,
                 "band": b, "band_threshold": band, "count": int(n), "total_delta": Decimal(tot),
                 "first_break_id": fid, "shape": {"kind": "band", "band": b, "systematic": False,
                                                 "median_shift": None if med is None else round(float(med), 6)}}
                for b, n, tot, fid, med in rows]

    def _break_sig_filter(self, run_id: str, g: dict):
        cond = [m.ReconciliationBreak.run_id == run_id, m.ReconciliationBreak.entity == g["entity"],
                m.ReconciliationBreak.field == g["field"], m.ReconciliationBreak.break_type == g["break_type"]]
        kind = g.get("kind", "exact")
        num, rel = self._NUM, self._REL.format(n=self._NUM)
        if kind == "shift":
            cond.append(text(f"(delta ~ {num} AND sign(delta::numeric) = :sg)").bindparams(sg=g["sign"]))
        elif kind == "band":
            cond.append(m.ReconciliationBreak.delta.is_not(None))
            cond.append(text(f"({self._BAND.format(rel=rel)}) = :bandname")
                        .bindparams(band=g["band_threshold"], bandname=g["band"]))
        else:
            cond.append(m.ReconciliationBreak.delta.is_(None) if g["delta"] is None
                        else m.ReconciliationBreak.delta == g["delta"])
        return and_(*cond)

    def break_ids(self, run_id: str, group: dict, limit: int) -> list[str]:
        with self._sf() as s:
            return list(s.scalars(select(m.ReconciliationBreak.break_id).where(self._break_sig_filter(run_id, group))
                                  .order_by(m.ReconciliationBreak.break_id).limit(limit)))

    @staticmethod
    def _break_from_row(row: m.ReconciliationBreak) -> ReconciliationBreak:
        b = ReconciliationBreak.model_validate(row.body)
        return b.model_copy(update={"cluster_id": row.cluster_id})

    def get_break(self, break_id: str) -> ReconciliationBreak:
        with self._sf() as s:
            row = s.get(m.ReconciliationBreak, break_id)
            if row is None:
                raise NotFound(break_id)
            return self._break_from_row(row)

    def breaks_for_cluster(self, run_id: str, cluster_id: str, limit: int) -> list[ReconciliationBreak]:
        with self._sf() as s:
            rows = s.scalars(select(m.ReconciliationBreak).where(m.ReconciliationBreak.run_id == run_id,
                                                                 m.ReconciliationBreak.cluster_id == cluster_id)
                             .order_by(m.ReconciliationBreak.break_id).limit(limit))
            return [self._break_from_row(r) for r in rows]

    def iter_breaks(self, run_id: str, *, exclude_clusters: set[str] | None = None,
                    batch: int = 2000) -> Iterator[ReconciliationBreak]:
        """Stream a run's breaks ordered by customer record (outcome aggregation)."""
        q = select(m.ReconciliationBreak).where(m.ReconciliationBreak.run_id == run_id)
        if exclude_clusters:
            q = q.where(or_(m.ReconciliationBreak.cluster_id.is_(None),
                            m.ReconciliationBreak.cluster_id.not_in(sorted(exclude_clusters))))
        q = q.order_by(m.ReconciliationBreak.record_key, m.ReconciliationBreak.break_id)
        with self._sf() as s:
            for row in s.execute(q.execution_options(yield_per=batch)).scalars():
                yield self._break_from_row(row)

    def count(self, cls: type, run_id: str, **filters: Any) -> int:
        q = select(func.count()).select_from(cls).where(cls.run_id == run_id)
        for k, v in filters.items():
            if v is not None:
                q = q.where(getattr(cls, k) == v)
        with self._sf() as s:
            return int(s.scalar(q) or 0)

    def save_outcomes(self, run_id: str, outcomes: Iterable[ConductOutcome], batch: int = 2000) -> int:
        n = 0
        with self._sf.begin() as s:
            s.execute(delete(m.ConductOutcomeRow).where(m.ConductOutcomeRow.run_id == run_id))
            buf: list[dict] = []
            for o in outcomes:
                buf.append({"case_id": o.case_id, "run_id": run_id, "record_key": o.record_key,
                            "impact_type": o.impact_type, "body": o.model_dump(mode="json")})
                if len(buf) >= batch:
                    s.execute(m.ConductOutcomeRow.__table__.insert(), buf)
                    n += len(buf)
                    buf = []
            if buf:
                s.execute(m.ConductOutcomeRow.__table__.insert(), buf)
                n += len(buf)
        return n

    def iter_outcomes(self, run_id: str, batch: int = 2000) -> Iterator[ConductOutcome]:
        q = select(m.ConductOutcomeRow.body).where(m.ConductOutcomeRow.run_id == run_id) \
            .order_by(m.ConductOutcomeRow.record_key)
        with self._sf() as s:
            for body in s.execute(q.execution_options(yield_per=batch)).scalars():
                yield ConductOutcome.model_validate(body)

    def save_live_archive_scores(self, run_id: str, artefact_id: str,
                                 scores: Iterable[tuple[str, float, str, dict]]) -> int:
        n = 0
        with self._sf.begin() as s:
            s.execute(delete(m.LiveArchiveScore).where(m.LiveArchiveScore.run_id == run_id,
                                                       m.LiveArchiveScore.artefact_id == artefact_id))
            pg = s.connection().connection.driver_connection
            with pg.cursor() as cur:
                with cur.copy("COPY live_archive_score (run_id, artefact_id, record_ref, live_probability, "
                              "recommendation, components) FROM STDIN") as cp:
                    for ref, p, rec, comps in scores:
                        cp.write_row((run_id, artefact_id, ref, p, rec, json.dumps(comps)))
                        n += 1
        return n

    def save_profiles(self, run_id: str, profiles: Iterable[Any]) -> int:
        n = 0
        with self._sf.begin() as s:
            s.execute(delete(m.ColumnProfile).where(m.ColumnProfile.run_id == run_id))
            for p in profiles:
                s.add(m.ColumnProfile(profile_id=stable_id("PRF", run_id, p.system.system_name, p.entity, p.column),
                                      run_id=run_id, entity=p.entity, column=p.column,
                                      system_name=p.system.system_name, body=p.model_dump(mode="json")))
                n += 1
        return n

    def save_clusters(self, clusters: Iterable[BreakCluster], groups: dict[str, dict] | None = None) -> int:
        """``groups``: cluster_id -> signature group from ``break_groups`` (membership is assigned in SQL)."""
        n = 0
        with self._sf.begin() as s:
            for c in clusters:
                s.merge(m.BreakCluster(cluster_id=c.cluster_id, run_id=c.run_id, signature=c.signature, field=c.field,
                                       count=c.count, total_delta=c.total_delta, status=c.status.value,
                                       body=c.model_dump(mode="json"), created_at=utc_now()))
                g = (groups or {}).get(c.cluster_id)
                where = self._break_sig_filter(c.run_id, g) if g else m.ReconciliationBreak.break_id.in_(c.break_ids)
                s.execute(update(m.ReconciliationBreak).where(where).values(cluster_id=c.cluster_id))
                self._audit(s, AuditEvent(audit_id=new_id("AUD"), run_id=c.run_id, who="break_cluster_agent",
                                          actor_type="agent", what="propose", entity_type="break_cluster",
                                          entity_id=c.cluster_id, after={"signature": c.signature, "count": c.count},
                                          agent="break_cluster_agent", reason=c.hypothesis))
                n += 1
        return n

    def dependency_graph(self, run_id: str, *, max_edges: int = 2000) -> dict:
        """What depends on what, from evidence already extracted: batch jobs -> programs -> datasets (JCL),
        business rules that feed other rules (code), and source -> target entities (approved / proposed mappings)."""
        nodes: dict[str, dict] = {}
        edges: dict[tuple[str, str, str], int] = {}

        def node(kind: str, label: str, role: str | None = None) -> str:
            nid = f"{kind}:{label}"
            nodes.setdefault(nid, {"id": nid, "kind": kind, "label": label, "system_role": role})
            return nid

        def edge(a: str, b: str, kind: str) -> None:
            edges[(a, b, kind)] = edges.get((a, b, kind), 0) + 1

        def canon(name: str) -> str:
            return name.upper().replace("-", "_").strip()

        with self._sf() as s:
            rules = s.execute(select(m.Rule.agent, m.Rule.canonical_name, m.Rule.inputs, m.Rule.context,
                                     m.Rule.system_role).where(m.Rule.run_id == run_id)
                              ).all()
            maps = s.execute(select(m.Mapping.source_entity, m.Mapping.target_entity, func.count())
                             .where(m.Mapping.run_id == run_id)
                             .group_by(m.Mapping.source_entity, m.Mapping.target_entity)).all()
        produced = {}
        for agent, cname, _inputs, ctx, role in rules:
            if agent != "jcl_agent":
                produced.setdefault((role, canon(cname)), cname)
        for agent, cname, inputs, ctx, role in rules:
            ctx = ctx or {}
            if agent == "jcl_agent" and ctx.get("program") and ctx.get("job"):
                job = node("job", ctx["job"], role)
                prog = node("program", ctx["program"], role)
                edge(job, prog, "runs")
                for dd in ctx.get("datasets") or []:
                    if dd.get("dsn"):
                        disp = str(dd.get("disp") or "").upper()
                        ds = node("dataset", dd["dsn"], role)
                        if disp.startswith(("NEW", "MOD", "(NEW", "(MOD")):
                            edge(prog, ds, "writes")
                        else:
                            edge(ds, prog, "reads")
            elif agent != "jcl_agent":
                for name in inputs or []:
                    src = produced.get((role, canon(str(name))))
                    if src and canon(src) != canon(cname):
                        edge(node("rule", src, role), node("rule", cname, role), "feeds")
        for se, te, n in maps:
            a, b = node("entity", f"source.{se}", "source"), node("entity", f"target.{te}", "target")
            edges[(a, b, "maps_to")] = int(n)
        ranked = sorted(edges.items(), key=lambda kv: -kv[1])
        kept = ranked[:max_edges]
        used = {x for (a, b, _k), _n in kept for x in (a, b)}
        return {"run_id": run_id, "nodes": [v for k, v in nodes.items() if k in used],
                "edges": [{"from": a, "to": b, "kind": k, "count": n} for (a, b, k), n in kept],
                "truncated": len(ranked) > max_edges, "total_edges": len(ranked)}

    def set_break_lineage(self, break_id: str, lineage: dict) -> None:
        with self._sf.begin() as s:
            s.execute(update(m.ReconciliationBreak).where(m.ReconciliationBreak.break_id == break_id)
                      .values(lineage=lineage))

    def save_conduct_cases(self, cases: Iterable[ConductCase], agent: str = "case_routing_agent") -> int:
        now = utc_now()
        items = [(dict(case_id=c.case_id, run_id=c.run_id, record_key=c.outcome.record_key,
                       impact_type=c.outcome.impact_type, detriment=c.outcome.detriment, band=c.band, route=c.route,
                       priority=c.priority, status=c.status.value, body=c.model_dump(mode="json"), decision=None,
                       decided_by=None, created_at=now),
                  c.outcome.evidence, c.status, {"impact": c.outcome.impact_type, "detriment": c.outcome.detriment,
                                                 "band": c.band}, c.outcome.rationale) for c in cases]
        return self._governed_bulk(run_id=items[0][0]["run_id"] if items else "-", agent=agent,
                                   entity_type="conduct_case", items=items)

    def save_stories(self, stories: Iterable[UserStory]) -> int:
        n = 0
        with self._sf.begin() as s:
            for st in stories:
                row = m.Story(story_id=st.story_id, run_id=st.run_id, rule_id=st.rule_id, status=st.status.value,
                              body=st.model_dump(mode="json"), created_at=utc_now())
                self._governed_write(s, run_id=st.run_id, agent="story_agent", entity_type="story",
                                     entity_id=st.story_id, row=row, evidence=st.evidence, status=st.status,
                                     summary={"title": st.title}, reason="generated from certified rule")
                n += 1
        return n

    def save_generated_code(self, code: GeneratedCode, evidence: Sequence[Evidence]) -> None:
        with self._sf.begin() as s:
            row = m.GeneratedCode(code_id=code.code_id, run_id=code.run_id, module_name=code.module_name,
                                  status=code.status.value, body=code.model_dump(mode="json"), created_at=utc_now())
            self._governed_write(s, run_id=code.run_id, agent="codegen_agent", entity_type="generated_code",
                                 entity_id=code.code_id, row=row, evidence=evidence, status=code.status,
                                 summary={"module": code.module_name, "validation": code.validation},
                                 reason="generated from certified mappings")

    # ================================================================== human decisions (certification)
    def apply_human_decision(self, run_id: str, item_type: str, item_id: str, decision: HumanDecision,
                             *, approve_values: frozenset[str] = frozenset({"approve", "accept_source", "accept_target",
                                                                             "override", "confirm", "accept_variance",
                                                                             "go", "remediate", "no_action"})) -> str:
        """Record a named human's decision and move the item to certified/rejected. Returns the new status."""
        task = self.task_for_item(item_type, item_id)
        validate_human_decision(item_type, decision.role, decision.decided_by,
                                HumanRole(task.required_role) if task is not None else None)
        validate_decision_value(item_type, decision.decision, decision.certified_expression)
        table = ENTITY_TABLES.get(item_type)
        new_status = ItemStatus.CERTIFIED if decision.decision in approve_values else ItemStatus.REJECTED
        with self._sf.begin() as s:
            before: dict[str, Any] = {}
            if table is not None:
                cls, pk = table
                row = s.get(cls, item_id)
                if row is None:
                    raise KeyError(f"{item_type} {item_id} not found")
                before = {"status": row.status}
                transition(ItemStatus(row.status), new_status, actor_type="human")
                row.status = new_status.value
                if hasattr(row, "decided_by"):
                    row.decided_by = decision.decided_by
                if item_type == "mapping":
                    row.approved_by, row.approved_at = decision.decided_by, decision.decided_at
                if item_type == "conduct_case":
                    row.decision = decision.decision
            if item_type == "differential":
                s.add(m.Adjudication(adjudication_id=new_id("ADJ"), run_id=run_id, differential_id=item_id,
                                     decision=decision.decision, decided_by=decision.decided_by,
                                     role=decision.role.value, rationale=decision.rationale,
                                     certified_expression=decision.certified_expression,
                                     decided_at=decision.decided_at))
            self._audit(s, AuditEvent(
                audit_id=new_id("AUD"), run_id=run_id, who=decision.decided_by, actor_type="human",
                what="certify" if new_status is ItemStatus.CERTIFIED else "reject", entity_type=item_type,
                entity_id=item_id, before=before,
                after={"status": new_status.value, "decision": decision.decision,
                       "certified_expression": decision.certified_expression, "role": decision.role.value},
                reason=decision.rationale,
            ))
        log_event(Event.HUMAN_DECISION_RECEIVED, item_type=item_type, item_id=item_id, decision=decision.decision,
                  role=decision.role.value)
        return new_status.value

    def certify_rule(self, run_id: str, rule_id: str, decision: HumanDecision, certified_expression: str,
                     differential_id: str | None) -> None:
        """Write the certified rule: the expression and the approver's name become permanent fact (Stage 4)."""
        # the decision's role was validated against the rule gate's review task when it was recorded; here the
        # control plane re-checks that a named human (never an agent or the system) made it
        validate_human_decision("rule", decision.role, decision.decided_by, decision.role)
        with self._sf.begin() as s:
            row = s.get(m.Rule, rule_id)
            if row is None:
                raise KeyError(rule_id)
            if not s.scalar(select(func.count()).select_from(m.Provenance)
                            .where(m.Provenance.entity_type == "rule", m.Provenance.entity_id == rule_id)):
                raise MissingProvenance(f"rule {rule_id} has no provenance; it cannot be certified")
            before = {"status": row.status, "expression": row.expression}
            transition(ItemStatus(row.status), ItemStatus.CERTIFIED, actor_type="human")
            row.status = ItemStatus.CERTIFIED.value
            row.certified_expression = certified_expression
            row.certified_by, row.certified_role, row.certified_at = (
                decision.decided_by, decision.role.value, decision.decided_at)
            self._audit(s, AuditEvent(audit_id=new_id("AUD"), run_id=run_id, who=decision.decided_by,
                                      actor_type="human", what="certify", entity_type="rule", entity_id=rule_id,
                                      before=before, after={"status": "certified",
                                                            "certified_expression": certified_expression,
                                                            "differential_id": differential_id},
                                      reason=decision.rationale))

    def mark_unresolved(self, run_id: str, item_type: str, item_ids: Iterable[str], reason: str) -> None:
        """No valid human decision: the item is unresolved. This is never approval."""
        table = ENTITY_TABLES.get(item_type)
        with self._sf.begin() as s:
            for item_id in item_ids:
                before = {}
                if table is not None:
                    row = s.get(table[0], item_id)
                    if row is None:
                        continue
                    before = {"status": row.status}
                    row.status = transition(ItemStatus(row.status), ItemStatus.UNRESOLVED, actor_type="system").value
                self._audit(s, AuditEvent(audit_id=new_id("AUD"), run_id=run_id, who="orchestrator_agent",
                                          actor_type="system", what="unresolved", entity_type=item_type,
                                          entity_id=item_id, before=before, after={"status": "unresolved"},
                                          reason=reason))

    # ================================================================== HITL queue
    def create_hitl_tasks(self, tasks: Iterable[HitlTask]) -> int:
        """Idempotent bulk insert (the gate node re-enters on resume): returns how many were new."""
        rows = [dict(task_id=t.task_id, run_id=t.run_id, gate=t.gate, item_type=t.item_type, item_id=t.item_id,
                     required_role=t.required_role.value, title=t.title, brief=t.brief, status="pending",
                     decision=None, decided_by=None, decided_at=None, created_at=t.created_at) for t in tasks]
        n = 0
        with self._sf.begin() as s:
            for i in range(0, len(rows), 2000):
                res = s.execute(pg_insert(m.HitlTask.__table__).on_conflict_do_nothing(index_elements=["task_id"])
                                .returning(m.HitlTask.task_id), rows[i:i + 2000])
                n += len(res.all())
        if n:
            log_event(Event.HUMAN_REVIEW_REQUIRED, count=n)
        return n

    def record_task_decision(self, task_id: str, decision: HumanDecision) -> m.HitlTask:
        with self._sf.begin() as s:
            task = s.get(m.HitlTask, task_id)
            if task is None:
                raise KeyError(task_id)
            if task.status != "pending":
                raise ControlPlaneViolation(f"task {task_id} is already {task.status}")
            validate_human_decision(task.item_type, decision.role, decision.decided_by, HumanRole(task.required_role))
            validate_decision_value(task.item_type, decision.decision, decision.certified_expression)
            task.decision = decision.model_dump(mode="json")
            task.decided_by, task.decided_at, task.status = decision.decided_by, decision.decided_at, "decided"
            self._audit(s, AuditEvent(audit_id=new_id("AUD"), run_id=task.run_id, who=decision.decided_by,
                                      actor_type="human", what="decide", entity_type="hitl_task", entity_id=task_id,
                                      after={"decision": decision.decision, "item_id": task.item_id},
                                      reason=decision.rationale))
            return task

    def close_gate_tasks(self, run_id: str, gate: str) -> None:
        with self._sf.begin() as s:
            s.execute(update(m.HitlTask).where(m.HitlTask.run_id == run_id, m.HitlTask.gate == gate,
                                               m.HitlTask.status == "pending").values(status="closed_unresolved"))

    def list_tasks(self, run_id: str | None = None, status: str | None = None, gate: str | None = None,
                   role: str | None = None, *, project_id: str | None = None, limit: int | None = None,
                   offset: int = 0) -> list[m.HitlTask]:
        q = select(m.HitlTask).order_by(m.HitlTask.created_at, m.HitlTask.task_id)
        if run_id:
            q = q.where(m.HitlTask.run_id == run_id)
        elif project_id:
            q = q.where(m.HitlTask.run_id.in_(select(m.IngestRun.run_id).where(m.IngestRun.project_id == project_id)))
        else:
            raise ScopeRequired("listing review tasks needs a run_id or project_id")
        if limit is not None:
            q = q.limit(limit).offset(offset)
        if status:
            q = q.where(m.HitlTask.status == status)
        if gate:
            q = q.where(m.HitlTask.gate == gate)
        if role:
            q = q.where(m.HitlTask.required_role == role)
        with self._sf() as s:
            return list(s.scalars(q))

    def task_for_item(self, item_type: str, item_id: str) -> m.HitlTask | None:
        with self._sf() as s:
            return s.scalars(select(m.HitlTask).where(m.HitlTask.item_type == item_type, m.HitlTask.item_id == item_id)
                             .order_by(m.HitlTask.created_at.desc())).first()

    # ================================================================== signals + telemetry
    def save_signal(self, sig: Signal) -> None:
        with self._sf.begin() as s:
            s.add(m.Signal(signal_id=sig.signal_id, run_id=sig.run_id, agent=sig.agent, tier=sig.tier.value,
                           signal_type=sig.signal_type.value, level=sig.level.value, message=sig.message,
                           item_refs=sig.item_refs[:500], data=sig.data, artefact_id=sig.artefact_id,
                           trace_id=sig.trace_id, created_at=sig.created_at))

    def list_signals(self, run_id: str, after_seq: int = 0, limit: int = 1000) -> list[m.Signal]:
        with self._sf() as s:
            return list(s.scalars(select(m.Signal).where(m.Signal.run_id == run_id, m.Signal.seq > after_seq)
                                  .order_by(m.Signal.seq).limit(limit)))

    def save_span(self, span: Any) -> None:
        with self._sf.begin() as s:
            s.merge(m.TraceSpan(**span.model_dump(exclude={"kind"}), kind=span.kind.value))

    def save_execution(self, ex: AgentExecution) -> None:
        with self._sf.begin() as s:
            s.merge(m.AgentExecution(execution_id=ex.execution_id, run_id=ex.run_id, agent_name=ex.agent_name,
                                     tier=ex.tier.value, status=ex.status.value, duration_ms=ex.duration_ms,
                                     trace_id=ex.trace_id, started_at=ex.started_at,
                                     record=ex.model_dump(mode="json")))

    def save_llm_call(self, rec: LLMCallRecord) -> None:
        with self._sf.begin() as s:
            s.add(m.LLMCall(call_id=rec.call_id, run_id=rec.run_id, agent=rec.agent, record=rec.model_dump(mode="json"),
                            estimated_cost=rec.estimated_cost, created_at=utc_now()))

    def save_headroom_event(self, run_id: str, agent: str, rec: HeadroomMetrics) -> None:
        with self._sf.begin() as s:
            s.add(m.HeadroomEvent(event_id=new_id("HRM"), run_id=run_id, agent=agent,
                                  record=rec.model_dump(mode="json"), created_at=utc_now()))

    def save_guardrail_event(self, run_id: str, agent: str | None, res: GuardrailResult) -> None:
        with self._sf.begin() as s:
            s.add(m.GuardrailEvent(event_id=new_id("GRD"), run_id=run_id, agent=agent, stage=res.stage,
                                   policy=res.policy, passed=res.passed, severity=res.severity, reason=res.reason,
                                   trace_id=context.get("trace_id"), created_at=utc_now()))

    def save_mcp_invocation(self, run_id: str | None, agent: str, server: str, tool: str, status: str,
                            record: dict) -> None:
        with self._sf.begin() as s:
            s.add(m.MCPInvocation(invocation_id=new_id("MCP"), run_id=run_id, agent=agent, server=server, tool=tool,
                                  status=status, record=record, created_at=utc_now()))

    def save_assurance_report(self, report: AssuranceReport) -> None:
        with self._sf.begin() as s:
            s.merge(m.AssuranceReport(report_id=report.report_id, run_id=report.run_id, passed=report.passed,
                                      readiness_score=report.readiness_score, body=report.model_dump(mode="json"),
                                      created_at=report.created_at))
            self._audit(s, AuditEvent(audit_id=new_id("AUD"), run_id=report.run_id, who="assurance_agent",
                                      actor_type="agent", what="assure", entity_type="assurance_report",
                                      entity_id=report.report_id,
                                      after={"passed": report.passed, "readiness": report.readiness_score},
                                      agent="assurance_agent", reason="; ".join(report.blockers)[:500]))

    def save_eval_result(self, eval_id: str, capability: str, passed: bool, metrics: dict, fingerprint: str) -> None:
        with self._sf.begin() as s:
            s.merge(m.EvalResult(eval_id=eval_id, capability=capability, passed=passed, metrics=metrics,
                                 config_fingerprint=fingerprint, created_at=utc_now()))

    # ================================================================== reads
    def rows(self, cls: type, run_id: str | None = None, *, project_id: str | None = None, limit: int | None = None,
             offset: int = 0, order_by: Any = None, **filters: Any) -> list[Any]:
        """Rows of a run-scoped table. A run or a project scope is mandatory - nothing reads across projects."""
        q = select(cls)
        if run_id is not None:
            q = q.where(cls.run_id == run_id)
        elif project_id is not None:
            q = q.where(cls.run_id.in_(select(m.IngestRun.run_id).where(m.IngestRun.project_id == project_id)))
        elif cls is not m.EvalResult:
            raise ScopeRequired(f"reading {cls.__tablename__} needs a run_id or project_id")
        for key, value in filters.items():
            if value is not None:
                q = q.where(getattr(cls, key) == value)
        if order_by is not None:
            q = q.order_by(order_by)
        if limit is not None:
            q = q.limit(limit).offset(offset)
        with self._sf() as s:
            return list(s.scalars(q))

    def run_project(self, run_id: str) -> str | None:
        with self._sf() as s:
            return s.scalar(select(m.IngestRun.project_id).where(m.IngestRun.run_id == run_id))

    def get(self, cls: type, key: str) -> Any:
        with self._sf() as s:
            return s.get(cls, key)

    def provenance_for(self, entity_type: str, entity_id: str) -> list[m.Provenance]:
        with self._sf() as s:
            return list(s.scalars(select(m.Provenance).where(m.Provenance.entity_type == entity_type,
                                                             m.Provenance.entity_id == entity_id)))

    def audit_for(self, entity_type: str | None = None, entity_id: str | None = None, run_id: str | None = None,
                  limit: int = 500, *, project_id: str | None = None, offset: int = 0) -> list[m.Audit]:
        q = select(m.Audit).order_by(m.Audit.when)
        if project_id and not run_id:
            q = q.where(or_(m.Audit.run_id.in_(select(m.IngestRun.run_id).where(m.IngestRun.project_id == project_id)),
                            and_(m.Audit.entity_type == "project", m.Audit.entity_id == project_id)))
        if entity_type:
            q = q.where(m.Audit.entity_type == entity_type)
        if entity_id:
            q = q.where(m.Audit.entity_id == entity_id)
        if run_id:
            q = q.where(m.Audit.run_id == run_id)
        with self._sf() as s:
            return list(s.scalars(q.limit(limit).offset(offset)))

    def completeness(self, run_id: str) -> dict[str, dict[str, int]]:
        """Provenance + audit completeness per governed entity type (assurance gate input)."""
        out: dict[str, dict[str, int]] = {}
        with self._sf() as s:
            for entity_type in m.GOVERNED_ENTITIES:
                cls, pk = ENTITY_TABLES[entity_type]
                ids = list(s.scalars(select(getattr(cls, pk)).where(cls.run_id == run_id)))
                if not ids:
                    out[entity_type] = {"rows": 0, "with_provenance": 0, "with_audit": 0}
                    continue
                prov = set(s.scalars(select(m.Provenance.entity_id).where(
                    m.Provenance.entity_type == entity_type, m.Provenance.entity_id.in_(ids))))
                aud = set(s.scalars(select(m.Audit.entity_id).where(
                    m.Audit.entity_type == entity_type, m.Audit.entity_id.in_(ids))))
                out[entity_type] = {"rows": len(ids), "with_provenance": len(prov), "with_audit": len(aud)}
        return out

    def telemetry_totals(self, run_id: str) -> dict[str, Any]:
        with self._sf() as s:
            spans = s.scalar(select(func.count()).select_from(m.TraceSpan).where(m.TraceSpan.run_id == run_id)) or 0
            execs = list(s.scalars(select(m.AgentExecution).where(m.AgentExecution.run_id == run_id)))
            no_trace = sum(1 for e in execs if not e.trace_id)
            guard = list(s.scalars(select(m.GuardrailEvent).where(m.GuardrailEvent.run_id == run_id)))
        return {"spans": spans, "executions": len(execs), "executions_without_trace": no_trace,
                "guardrail_events": len(guard), "guardrail_blocks": sum(1 for g in guard if not g.passed)}


    # ================================================================== durable jobs
    def enqueue_job(self, *, job_id: str, kind: str, payload: dict, created_by: str, project_id: str | None = None,
                    run_id: str | None = None, upload_id: str | None = None, max_attempts: int = 3) -> str:
        now = utc_now()
        with self._sf.begin() as s:
            s.add(m.Job(job_id=job_id, kind=kind, project_id=project_id, run_id=run_id, upload_id=upload_id,
                        payload=payload, status="queued", attempts=0, max_attempts=max_attempts, available_at=now,
                        result={}, created_by=created_by, created_at=now, updated_at=now))
        return job_id

    def claim_job(self, worker: str, lease_seconds: int, job_id: str | None = None,
                  kinds: Sequence[str] | None = None) -> m.Job | None:
        """Claim the next runnable job (or a specific one): queued, or running with an expired lease (a worker
        died mid-job). ``FOR UPDATE SKIP LOCKED`` lets any number of workers claim concurrently."""
        now = utc_now()
        with self._sf.begin() as s:
            q = select(m.Job).where(or_(and_(m.Job.status == "queued", m.Job.available_at <= now),
                                        and_(m.Job.status == "running", m.Job.locked_until < now)))
            if job_id:
                q = q.where(m.Job.job_id == job_id)
            if kinds:
                q = q.where(m.Job.kind.in_(list(kinds)))
            job = s.scalars(q.order_by(m.Job.available_at).limit(1).with_for_update(skip_locked=True)).first()
            if job is None:
                return None
            job.status, job.locked_by = "running", worker
            job.locked_until = now + timedelta(seconds=lease_seconds)
            job.attempts += 1
            job.updated_at = now
            return job

    def heartbeat_job(self, job_id: str, worker: str, lease_seconds: int) -> bool:
        with self._sf.begin() as s:
            res = s.execute(update(m.Job).where(m.Job.job_id == job_id, m.Job.locked_by == worker,
                                                m.Job.status == "running")
                            .values(locked_until=utc_now() + timedelta(seconds=lease_seconds), updated_at=utc_now()))
            return bool(res.rowcount)

    def finish_job(self, job_id: str, *, ok: bool, result: dict | None = None, error: str | None = None,
                   retry_in: float | None = None) -> None:
        with self._sf.begin() as s:
            job = s.get(m.Job, job_id)
            if job is None:
                return
            job.updated_at = utc_now()
            job.locked_by, job.locked_until = None, None
            if ok:
                job.status, job.result, job.error = "succeeded", result or {}, None
            elif retry_in is not None and job.attempts < job.max_attempts:
                job.status, job.error = "queued", (error or "")[:4000]
                job.available_at = utc_now() + timedelta(seconds=retry_in)
            else:
                job.status, job.error = "failed", (error or "")[:4000]

    def get_job(self, job_id: str) -> m.Job | None:
        with self._sf() as s:
            return s.get(m.Job, job_id)

    def jobs_for(self, *, run_id: str | None = None, upload_id: str | None = None) -> list[m.Job]:
        q = select(m.Job).order_by(m.Job.created_at.desc())
        if run_id:
            q = q.where(m.Job.run_id == run_id)
        if upload_id:
            q = q.where(m.Job.upload_id == upload_id)
        with self._sf() as s:
            return list(s.scalars(q.limit(50)))

    # ================================================================== sign-in lockout
    def login_state(self, username: str) -> m.LoginAttempt | None:
        with self._sf() as s:
            return s.get(m.LoginAttempt, username)

    def record_login(self, username: str, ok: bool, max_failures: int, lockout_seconds: int) -> None:
        with self._sf.begin() as s:
            row = s.get(m.LoginAttempt, username) or m.LoginAttempt(username=username, failures=0)
            s.add(row)
            now = utc_now()
            row.last_attempt_at = now
            if ok:
                row.failures, row.locked_until = 0, None
            else:
                row.failures = (row.failures or 0) + 1
                if row.failures >= max_failures:
                    row.locked_until = now + timedelta(seconds=lockout_seconds)
                    row.failures = 0
            self._audit(s, AuditEvent(audit_id=new_id("AUD"), run_id="-", who=username, actor_type="human",
                                      what="sign_in" if ok else "sign_in_failed", entity_type="user",
                                      entity_id=username, reason="administrator sign-in"))


def now() -> datetime:
    return utc_now()
