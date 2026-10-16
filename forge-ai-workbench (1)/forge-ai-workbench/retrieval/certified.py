"""Certified retrieval: builds the search index from *certified* knowledge-core rows only.

    Certified truth -> PostgreSQL (this module reads it)
    Raw evidence    -> object storage (reached through provenance, never indexed as truth)
    Search          -> PostgreSQL full-text + optional pgvector, scoped to project and run
"""
from __future__ import annotations

from sqlalchemy import Engine

from config.settings import RetrievalSettings
from db import models as m
from db.repository import KnowledgeCore
from schemas.common import ItemStatus
from tools.search.index import SearchBackend, SearchDocument
from tools.search.pgvector import PgSearchIndex


def certified_documents(kc: KnowledgeCore, run_id: str) -> list[SearchDocument]:
    docs: list[SearchDocument] = []
    for r in kc.rows(m.Rule, run_id, status=ItemStatus.CERTIFIED.value):
        prov = kc.provenance_for("rule", r.rule_id)
        where = "; ".join(f"{p.locator.get('file')} {p.locator.get('line_start', '')}" for p in prov)
        docs.append(SearchDocument(
            doc_id=f"rule:{r.rule_id}", kind="certified_rule", title=f"{r.canonical_name} ({r.domain or 'general'})",
            text=(f"{r.canonical_name.replace('_', ' ')} {r.name}. Certified expression: {r.certified_expression}. "
                  f"Conditions: {' AND '.join(r.conditions) or 'always'}. System: {r.system_name} ({r.system_role}). "
                  f"Certified by {r.certified_by} ({r.certified_role}). Evidence: {where}."),
            ref=f"rule:{r.rule_id}", metadata={"run_id": r.run_id, "certified_by": r.certified_by}))
    for a in kc.rows(m.Adjudication, run_id):
        d = kc.get(m.Differential, a.differential_id)
        if d is None:
            continue
        docs.append(SearchDocument(
            doc_id=f"adjudication:{a.adjudication_id}", kind="adjudication",
            title=f"Decision on {d.canonical_name.replace('_', ' ')}",
            text=(f"{d.canonical_name.replace('_', ' ')}: {d.body.get('explanation', '')} Decision: {a.decision}. "
                  f"Certified expression: {a.certified_expression or '-'}. Decided by {a.decided_by} ({a.role}). "
                  f"Rationale: {a.rationale}"),
            ref=f"differential:{d.differential_id}", metadata={"run_id": a.run_id}))
    for mp in kc.rows(m.Mapping, run_id, status=ItemStatus.CERTIFIED.value):
        docs.append(SearchDocument(
            doc_id=f"mapping:{mp.mapping_id}", kind="mapping",
            title=f"Mapping {mp.source_entity}.{mp.source_field} to {mp.target_entity}.{mp.target_field}",
            text=(f"{mp.source_system} {mp.source_entity}.{mp.source_field} maps to {mp.target_system} "
                  f"{mp.target_entity}.{mp.target_field} using {mp.transformation.get('description', '')}. "
                  f"Approved by {mp.approved_by}."),
            ref=f"mapping:{mp.mapping_id}", metadata={"run_id": mp.run_id}))
    return docs


class CertifiedRetrieval:
    def __init__(self, kc: KnowledgeCore, engine: Engine, settings: RetrievalSettings, *, environment: str = "local",
                 backend: SearchBackend | None = None) -> None:
        self.kc = kc
        self.backend = backend or PgSearchIndex(engine, settings, environment=environment)

    def refresh(self, run_id: str) -> int:
        project_id = self.kc.run_project(run_id)
        if project_id is None:
            raise KeyError(run_id)
        docs = certified_documents(self.kc, run_id)
        self.backend.index(docs, project_id=project_id, run_id=run_id)
        return len(docs)

    def search(self, query: str, *, project_id: str, run_id: str | None = None, top: int = 5):
        return self.backend.search(query, project_id=project_id, run_id=run_id, top=top)
