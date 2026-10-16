"""API: OpenAPI contract parity, administrator sign-in, projects, chunked uploads, scoping and isolation."""
import re

from fastapi.testclient import TestClient

from api.app import contract_paths, create_app
from services.run_service import sample_bundle

PASSWORD = "test-admin-password"


def _app_paths(app) -> set[tuple[str, str]]:
    return {(path, method.upper()) for path, ops in app.openapi()["paths"].items() for method in ops}


def _client(ws) -> TestClient:
    return TestClient(create_app(ws.settings, ws.services, ws.runs, start_workers=False))


def _auth(c: TestClient) -> dict:
    r = c.post("/v1/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_implementation_matches_the_openapi_contract(golden):
    app = create_app(golden.settings, golden.services, golden.runs, start_workers=False)
    spec, impl = contract_paths(), _app_paths(app)
    assert spec == impl, {"missing_in_app": spec - impl, "not_in_spec": impl - spec}
    assert all(re.match(r"^/v1/", p) for p, _ in spec)


def test_only_the_signed_in_administrator_gets_in(golden):
    c = _client(golden)
    assert c.get("/v1/health").status_code == 200
    assert c.get("/v1/projects").status_code == 401
    assert c.get("/v1/projects", headers={"X-Workbench-User": "someone"}).status_code == 401   # no persona headers
    assert c.post("/v1/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    h = _auth(c)
    assert c.get("/v1/auth/me", headers=h).json() == {"username": "admin", "roles": ["admin"]}
    assert c.get("/v1/projects", headers={"Authorization": "Bearer tampered.token.value"}).status_code == 401


def test_repeated_failed_sign_ins_lock_the_account(ws):
    c = _client(ws)
    for _ in range(ws.settings.auth.max_failed_logins):
        c.post("/v1/auth/login", json={"username": "admin", "password": "nope"})
    assert c.post("/v1/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 429


def test_project_portfolio_and_chunked_upload_end_to_end(ws):
    c = _client(ws)
    h = _auth(c)
    meta = c.get("/v1/meta", headers=h).json()
    assert "Policy" in meta["business_domains"] and meta["upload_chunk_bytes"] > 0
    bad = c.post("/v1/projects", json={"name": "X", "business_domain": "Not a domain"}, headers=h)
    assert bad.status_code == 422
    proj = c.post("/v1/projects", json={"name": "Capsil", "business_domain": "Policy",
                                        "source_technologies": ["JCL"], "target_technologies": ["PHP"],
                                        "description": "JCL estate"}, headers=h)
    assert proj.status_code == 201
    pid = proj.json()["project_id"]
    assert c.post("/v1/projects", json={"name": "capsil", "business_domain": "Policy"}, headers=h).status_code == 422
    data, name = sample_bundle("source")
    up = c.post(f"/v1/projects/{pid}/uploads", json={"system_name": "Capita Policy Admin", "system_role": "source",
                                                     "filename": name, "size_bytes": len(data)}, headers=h).json()
    size = up["chunk_bytes"]
    for i in range(up["chunks_expected"]):
        r = c.put(f"/v1/uploads/{up['upload_id']}/chunks/{i}", content=data[i * size:(i + 1) * size], headers=h)
        assert r.status_code == 200, r.text
    done = c.post(f"/v1/uploads/{up['upload_id']}/complete", headers=h)
    assert done.status_code == 202 and done.json()["status"] == "validating"
    ws.runs.worker.drain()
    assert c.get(f"/v1/uploads/{up['upload_id']}", headers=h).json()["status"] == "ready"
    port = c.get("/v1/portfolio", headers=h).json()
    row = next(p for p in port["projects"] if p["project_id"] == pid)
    assert port["kpis"]["active_projects"] >= 1 and row["stage"] == "ready" and row["source_technologies"] == ["JCL"]


def test_reviews_are_project_scoped_and_isolated(golden):
    c = _client(golden)
    h = _auth(c)
    pid, run_id = golden.project_id, golden.run_id
    diffs = c.get(f"/v1/projects/{pid}/differentials", params={"run_id": run_id}, headers=h).json()
    assert diffs
    other = c.post("/v1/projects", json={"name": "Another system", "business_domain": "Claims"}, headers=h).json()
    # the other project sees none of this project's findings, evidence, runs or answers
    assert c.get(f"/v1/projects/{other['project_id']}/differentials", headers=h).json() == []
    assert c.get(f"/v1/projects/{other['project_id']}/differentials", params={"run_id": run_id},
                 headers=h).status_code == 404
    rule = c.get(f"/v1/projects/{pid}/rules", params={"run_id": run_id, "status": "certified"}, headers=h).json()[0]
    assert c.get(f"/v1/projects/{pid}/evidence/rule/{rule['rule_id']}", headers=h).json()[0]["locator"]["file"]
    assert c.get(f"/v1/projects/{other['project_id']}/evidence/rule/{rule['rule_id']}", headers=h).json() == []
    assert c.post(f"/v1/projects/{other['project_id']}/assistant/ask",
                  json={"question": "What is the certified administration fee?"}, headers=h).status_code == 409


def test_dashboard_assistant_evidence_and_events(golden):
    c = _client(golden)
    h = _auth(c)
    pid, run_id = golden.project_id, golden.run_id
    ready = c.get(f"/v1/projects/{pid}/readiness", params={"run_id": run_id}, headers=h).json()
    assert ready["assurance_passed"] is True and ready["readiness_score"] > 0.9
    ans = c.post(f"/v1/projects/{pid}/assistant/ask", json={"question": "What is the certified administration fee?",
                                                            "run_id": run_id}, headers=h).json()
    assert not ans["refused"] and ans["citations"]
    refused = c.post(f"/v1/projects/{pid}/assistant/ask", json={"question": "What is the weather in Paris?"},
                     headers=h).json()
    assert refused["refused"]
    events = c.get(f"/v1/runs/{run_id}/events", params={"once": "true"}, headers=h)
    assert events.status_code == 200 and "event: " in events.text
    token = h["Authorization"].split()[1]
    assert c.get(f"/v1/runs/{run_id}/events", params={"once": "true", "access_token": token}).status_code == 200
    trace = c.get(f"/v1/runs/{run_id}/trace", params={"limit": 1000}, headers=h).json()
    assert {"run", "graph", "agent", "guardrail"} <= {s["kind"] for s in trace}
    overview = c.get(f"/v1/projects/{pid}/overview", headers=h).json()
    assert overview["latest_run"]["status"] == "completed" and overview["counts"]["certified_rules"] > 0
    breaks = c.get(f"/v1/projects/{pid}/reconciliation/breaks", params={"run_id": run_id, "limit": 2}, headers=h).json()
    assert len(breaks) == 2
    agents = c.get("/v1/agents", headers=h).json()
    assert len(agents) == 32 and any(a["extractor"] and "cobol_program" in a["handles"] for a in agents)
