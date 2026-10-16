"""AI Workbench - operator CLI.

    python cli.py serve [--no-workers]          # API on http://127.0.0.1:8000 (docs at /v1/docs)
    python cli.py worker                        # job worker (uploads validation, pipeline runs, resumes)
    python cli.py migrate                       # apply database migrations (PostgreSQL)
    python cli.py hash-password                 # PBKDF2 hash for AIWB_ADMIN_PASSWORD_HASH (reads stdin / prompt)
    python cli.py projects                      # list projects
    python cli.py demo [--project NAME]         # create a project, upload the sample estate, start a run
    python cli.py status RUN_ID                 # run status, current gate, progress, pending reviews
    python cli.py tasks RUN_ID                  # the open human-review queue
    python cli.py decide TASK_ID DECISION --as "Name" --role actuary_sme [--rationale ...] [--expression ...]
    python cli.py resume RUN_ID --as "Name"     # close the gate; undecided items become unresolved
    python cli.py halt RUN_ID --as "Name"       # kill-switch
    python cli.py evals [--update-baseline]     # evaluation suite + release gate
    python cli.py llm-check                     # one governed test call to the configured LLM provider
"""
from __future__ import annotations

import argparse
import getpass
import json
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _runs():
    from config.settings import get_settings
    from services.run_service import RunService
    from services.runtime import build_services
    return RunService(build_services(get_settings()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aiwb", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-workers", action="store_true", help="do not run embedded job workers in the API process")
    sub.add_parser("worker")
    sub.add_parser("migrate")
    sub.add_parser("hash-password")
    sub.add_parser("projects")
    dm = sub.add_parser("demo")
    dm.add_argument("--project", default="Sample estate")
    for name in ("status", "tasks"):
        sub.add_parser(name).add_argument("run_id")
    d = sub.add_parser("decide")
    d.add_argument("task_id")
    d.add_argument("decision")
    d.add_argument("--as", dest="who", required=True)
    d.add_argument("--role", required=True)
    d.add_argument("--rationale", default="")
    d.add_argument("--expression", default=None)
    for name in ("resume", "halt"):
        p = sub.add_parser(name)
        p.add_argument("run_id")
        p.add_argument("--as", dest="who", required=True)
    sub.add_parser("llm-check")
    e = sub.add_parser("evals")
    e.add_argument("--update-baseline", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        import os
        import uvicorn
        if a.no_workers:
            os.environ["AIWB__JOBS__EMBEDDED_WORKER"] = "false"
        uvicorn.run("api.main:app", host=a.host, port=a.port)
        return 0
    if a.cmd == "migrate":
        from config.settings import get_settings
        from db.migrate import current, upgrade
        from db.session import make_engine
        engine = make_engine(get_settings())
        upgrade(engine)
        print(f"database at revision {current(engine)}")
        return 0
    if a.cmd == "hash-password":
        from api.auth import hash_password
        pw = sys.stdin.readline().rstrip("\n") if not sys.stdin.isatty() else getpass.getpass("Admin password: ")
        if len(pw) < 12:
            print("password must be at least 12 characters", file=sys.stderr)
            return 1
        print(hash_password(pw))
        return 0
    if a.cmd == "evals":
        from evals.run_all import run
        return run(update_baseline=a.update_baseline)
    if a.cmd == "llm-check":
        return _llm_check()
    runs = _runs()
    if a.cmd == "worker":
        return _worker(runs)
    if a.cmd == "projects":
        for p in runs.s.kc.list_projects():
            print(f"{p.project_id}  {p.business_domain:12} {p.name}")
        return 0
    if a.cmd == "demo":
        from services.run_service import sample_bundle
        proj = runs.create_project(name=a.project, business_domain="Policy", source_technologies=["COBOL", "JCL"],
                                   target_technologies=["PHP"], description="sample estate", user="cli-operator")
        ids = []
        for role, system in (("source", "Legacy policy admin"), ("target", "Target platform")):
            data, filename = sample_bundle(role)
            up = runs.upload_bytes(proj.project_id, data, filename, system, role, "cli-operator")
            print(f"{role} upload {up.upload_id}: {up.status}")
            ids.append(up.upload_id)
        run_id = runs.start_run(proj.project_id, ids, "cli-operator", background=False)
        print(json.dumps(runs.status(run_id), indent=1, default=str))
        return 0
    if a.cmd == "status":
        print(json.dumps(runs.status(a.run_id), indent=1, default=str))
        return 0
    if a.cmd == "tasks":
        for t in runs.s.kc.list_tasks(a.run_id, status="pending"):
            print(f"{t.task_id}  [{t.required_role}]  {t.item_type:14} {t.title}")
        return 0
    if a.cmd == "decide":
        from db import models as m
        from schemas.common import HumanRole
        from schemas.differential import HumanDecision
        task = runs.s.kc.get(m.HitlTask, a.task_id)
        if task is None:
            print("unknown task")
            return 1
        out = runs.decide_task(a.task_id, HumanDecision(item_id=task.item_id, item_type=task.item_type,
                                                         decision=a.decision, decided_by=a.who, role=HumanRole(a.role),
                                                         rationale=a.rationale, certified_expression=a.expression),
                               background=False)
        print(json.dumps(out, indent=1, default=str))
        return 0
    if a.cmd == "resume":
        runs.resume(a.run_id, closed_by=a.who, background=False)
        print(json.dumps(runs.status(a.run_id), indent=1, default=str))
        return 0
    if a.cmd == "halt":
        runs.halt(a.run_id, a.who)
        print("halt requested")
        return 0
    return 1


def _worker(runs) -> int:
    """A dedicated worker process (Container Apps / k8s); scales on queue depth (KEDA) independently of the API."""
    import os
    import threading

    from services.jobs import Worker
    settings = runs.s.settings.jobs
    workers = [Worker(runs.s.kc, settings, runs.handlers, name=f"worker-{os.getpid()}-{i}")
               for i in range(max(1, settings.worker_concurrency))]
    threads = [threading.Thread(target=w.run_forever, name=w.name, daemon=True) for w in workers]

    def stop(*_: object) -> None:
        for w in workers:
            w.stop()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for t in threads:
        t.start()
    print(f"{len(workers)} worker(s) polling ({settings.backend})")
    while any(t.is_alive() for t in threads):
        time.sleep(1)
    return 0


def _llm_check() -> int:
    """Sends one small request through the full gateway (guardrails, Headroom, budgets, cost accounting)."""
    from pydantic import BaseModel

    from config.settings import get_settings
    from context.headroom.policy import ContentClass
    from reasoning.llm.factory import resolve_model
    from reasoning.llm.gateway import Evidence
    from services.runtime import build_services

    class Answer(BaseModel):
        answer: str
        citations: list[str] = []
        refused: bool = False
        refusal_reason: str | None = None

    settings = get_settings()
    model = resolve_model(settings)
    print(f"provider={settings.llm.provider} profile={model.name} model={model.deployment or '-'}")
    services = build_services(settings)
    res = services.llm.call(agent="assistant_agent", run_id="LLM-CHECK", purpose="compose_cited_answer",
                            variables={"question": "What is the certified administration fee?"},
                            evidence=[Evidence("rule:R-1", "[rule:R-1] ADMIN FEE certified expression 25.00",
                                               ContentClass.SEARCH_RESULT)], output_model=Answer)
    r = res.record
    print(f"status={r.status} latency={r.latency_ms:.0f}ms input={r.input_tokens} sent={r.compressed_input_tokens} "
          f"output={r.output_tokens} cost={r.estimated_cost}")
    if res.parsed is not None:
        print(f"answer={res.parsed.answer!r} citations={res.parsed.citations}")
    else:
        print(f"error={r.error}")
    return 0 if r.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
