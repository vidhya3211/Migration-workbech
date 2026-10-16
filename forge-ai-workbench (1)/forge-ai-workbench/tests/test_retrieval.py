"""Certified retrieval on PostgreSQL (full-text, project-scoped) and the embedding clients."""
from __future__ import annotations

import httpx
import pytest

from config.settings import RetrievalSettings
from retrieval.embeddings import OpenAIEmbeddings, build_embeddings
from tools.search.index import SearchDocument


def test_openai_embeddings_are_ordered_and_dimension_checked(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed = {}

    def respond(req: httpx.Request) -> httpx.Response:
        observed["url"] = str(req.url)
        observed["authorization"] = req.headers["Authorization"]
        observed["body"] = req.read().decode()
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [2.0] * 1536},
                                                  {"index": 0, "embedding": [1.0] * 1536}]})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    vectors = OpenAIEmbeddings(RetrievalSettings(), client=client).embed(["first", "second"])
    assert observed["url"] == "https://api.openai.com/v1/embeddings"
    assert observed["authorization"] == "Bearer test-key"
    assert '"model":"text-embedding-3-small"' in observed["body"]
    assert len(vectors) == 2 and vectors[0][0] == 1.0 and vectors[1][0] == 2.0


def test_embeddings_fail_explicitly_for_invalid_response(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    client = httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]})))
    with pytest.raises(RuntimeError, match="invalid vector"):
        OpenAIEmbeddings(RetrievalSettings(), client=client).embed(["text"])


def test_public_embeddings_refused_in_production_without_approval():
    with pytest.raises(ValueError):
        build_embeddings(RetrievalSettings(embedding_provider="openai"), environment="production")


def test_full_text_search_is_scoped_to_project_and_run(ws):
    kc = ws.services.kc
    backend = ws.services.retrieval.backend
    a = ws.project("Search A")
    b = ws.project("Search B")
    from graph.pipeline import resolve, pipeline_hash
    snap = resolve(None, "default")
    for pid, run in ((a, "RUN-A"), (b, "RUN-B")):
        kc.create_run(run, "t", "s", "t", project_id=pid, pipeline=snap, pipeline_hash=pipeline_hash(snap), steps_total=1)
    backend.index([SearchDocument("rule:A1", "certified_rule", "Administration fee", "admin fee is 25.00 monthly",
                                  "rule:A1")], project_id=a, run_id="RUN-A")
    backend.index([SearchDocument("rule:B1", "certified_rule", "Administration fee", "admin fee is 30.00 monthly",
                                  "rule:B1")], project_id=b, run_id="RUN-B")
    hits = ws.services.retrieval.search("what is the administration fee", project_id=a)
    assert [h.doc.doc_id for h in hits] == ["rule:A1"] and hits[0].score > 0
    assert [h.doc.doc_id for h in ws.services.retrieval.search("administration fee", project_id=b,
                                                               run_id="RUN-B")] == ["rule:B1"]
    assert ws.services.retrieval.search("weather in paris", project_id=a) == []
