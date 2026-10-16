"""PostgreSQL search over the certified core: full-text (tsvector + GIN) always, pgvector semantic ranking when
the ``vector`` extension and an embedding deployment are available (Azure Database for PostgreSQL + Azure
OpenAI). Every query is scoped to a project (and optionally a run) - one project can never retrieve another's
certified rules.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Engine, bindparam, text

from config.settings import RetrievalSettings
from tools.search.index import SearchDocument, SearchHit, terms


def vector_available(engine: Engine) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass('public.retrieval_embedding') IS NOT NULL")).scalar())


class PgSearchIndex:
    requires_term_overlap = True

    def __init__(self, engine: Engine, settings: RetrievalSettings, *, environment: str, embeddings=None) -> None:
        self.engine = engine
        self.settings = settings
        self.minimum_score = settings.min_text_rank
        self.embeddings = embeddings
        self.semantic = False
        if settings.semantic:
            if embeddings is None:
                from retrieval.embeddings import build_embeddings
                self.embeddings = build_embeddings(settings, environment=environment)
            self.semantic = vector_available(engine)
        if self.semantic:
            self.minimum_score = settings.min_similarity
            self.requires_term_overlap = False

    def index(self, docs: list[SearchDocument], *, project_id: str, run_id: str) -> None:
        rows = [{"doc_id": d.doc_id, "project_id": project_id, "run_id": run_id, "kind": d.kind, "title": d.title,
                 "content": d.text, "ref": d.ref, "metadata": json.dumps(d.metadata)} for d in docs]
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM retrieval_document WHERE run_id = :r"), {"r": run_id})
            if rows:
                conn.execute(text("INSERT INTO retrieval_document (doc_id, project_id, run_id, kind, title, content, "
                                  "ref, metadata) VALUES (:doc_id, :project_id, :run_id, :kind, :title, :content, :ref, "
                                  "CAST(:metadata AS jsonb)) ON CONFLICT (doc_id) DO UPDATE SET project_id = "
                                  "EXCLUDED.project_id, run_id = EXCLUDED.run_id, kind = EXCLUDED.kind, title = "
                                  "EXCLUDED.title, content = EXCLUDED.content, ref = EXCLUDED.ref, metadata = "
                                  "EXCLUDED.metadata"), rows)
            if self.semantic and docs:
                vectors = self.embeddings.embed([d.title + "\n" + d.text for d in docs])
                conn.execute(text("INSERT INTO retrieval_embedding (doc_id, embedding) VALUES (:d, CAST(:e AS vector)) "
                                  "ON CONFLICT (doc_id) DO UPDATE SET embedding = EXCLUDED.embedding"),
                             [{"d": d.doc_id, "e": _vector_literal(v)} for d, v in zip(docs, vectors, strict=True)])

    def search(self, query: str, *, project_id: str, run_id: str | None = None, top: int = 5,
               kinds: set[str] | None = None) -> list[SearchHit]:
        if top <= 0:
            return []
        where = ["d.project_id = :p"]
        params: dict[str, Any] = {"p": project_id, "top": top}
        if run_id:
            where.append("d.run_id = :r")
            params["r"] = run_id
        if kinds:
            where.append("d.kind IN :kinds")
            params["kinds"] = sorted(kinds)
        if self.semantic:
            params["e"] = _vector_literal(self.embeddings.embed([query])[0])
            sql = ("SELECT d.doc_id, d.kind, d.title, d.content, d.ref, d.metadata, "
                   "1 - (x.embedding <=> CAST(:e AS vector)) AS score FROM retrieval_document d "
                   "JOIN retrieval_embedding x ON x.doc_id = d.doc_id WHERE " + " AND ".join(where) +
                   " ORDER BY x.embedding <=> CAST(:e AS vector), d.doc_id LIMIT :top")
        else:
            words = terms(query)
            if not words:
                return []
            params["q"] = " | ".join(sorted(set(words)))
            sql = ("SELECT d.doc_id, d.kind, d.title, d.content, d.ref, d.metadata, "
                   "ts_rank_cd(d.search_vector, q, 32) AS score FROM retrieval_document d, "
                   "to_tsquery('english', :q) q WHERE d.search_vector @@ q AND " + " AND ".join(where) +
                   " ORDER BY score DESC, d.doc_id LIMIT :top")
        stmt = text(sql)
        if kinds:
            stmt = stmt.bindparams(bindparam("kinds", expanding=True))
        with self.engine.connect() as conn:
            rows = conn.execute(stmt, params).mappings().all()
        return [SearchHit(SearchDocument(doc_id=r["doc_id"], kind=r["kind"], title=r["title"], text=r["content"],
                                         ref=r["ref"], metadata=r["metadata"] or {}), float(r["score"])) for r in rows]


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(value) for value in vector) + "]"
