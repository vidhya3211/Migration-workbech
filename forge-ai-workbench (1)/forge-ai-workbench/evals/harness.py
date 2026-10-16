"""Evaluation harness: an isolated workspace, the golden pipeline run, and result types.

Every workspace gets its own PostgreSQL database (created on the server named by ``AIWB_TEST_DATABASE_URL``,
a URL with rights to CREATE DATABASE) and its own object-store directory, so evaluations and tests never touch
real data and can run in parallel. The golden pipeline runs the *real* path: a project, chunked uploads,
upload validation and run jobs, the compiled pipeline graph. Human gates are answered from reviewed golden
decisions, attributed to ``golden-dataset:<gate>`` - an identity the human-policy guardrail refuses in
production, so eval fixtures can never decide anything real.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import Settings, load_settings
from reasoning.deterministic.naming import NAMING_VERSION
from reasoning.llm.prompts import all_prompts
from schemas.common import HumanRole
from schemas.differential import HumanDecision
from services.run_service import RunService, sample_bundle
from services.runtime import Services, build_services

DATASETS = Path(__file__).resolve().parent / "datasets"
REPORTS = Path(__file__).resolve().parent / "reports"
ADMIN_DB_ENV = "AIWB_TEST_DATABASE_URL"

GATE_ROLES = {"rule_adjudication": HumanRole.ACTUARY_SME, "mapping_approval": HumanRole.DATA_STEWARD,
              "data_quality_review": HumanRole.DATA_STEWARD, "reconciliation_review": HumanRole.RECONCILIATION_ANALYST,
              "conduct_review": HumanRole.CONDUCT_OFFICER, "go_no_go": HumanRole.MIGRATION_LEAD}
GOLDEN_DECISIONS = {"differential": "accept_source", "rule": "approve", "mapping": "approve", "entity_match": "approve",
                    "cohort": "approve", "break_cluster": "accept_variance", "conduct_case": "remediate", "go_no_go": "go"}


def dataset(name: str) -> dict:
    return json.loads((DATASETS / f"{name}.json").read_text(encoding="utf-8"))


@dataclass
class CapabilityResult:
    capability: str
    metrics: dict[str, float]
    details: dict = field(default_factory=dict)
    duration_s: float = 0.0


def admin_url() -> str:
    url = os.environ.get(ADMIN_DB_ENV)
    if not url:
        raise RuntimeError(f"set {ADMIN_DB_ENV} to a PostgreSQL URL that may CREATE DATABASE "
                           "(e.g. postgresql+psycopg://postgres@localhost:5432/postgres) to run evaluations and tests")
    return url


def _plain(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def create_database(name: str) -> str:
    import psycopg
    base = admin_url()
    with psycopg.connect(_plain(base), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    head, _, _ = base.rpartition("/")
    return f"{head}/{name}"


def drop_database(name: str) -> None:
    import psycopg
    with psycopg.connect(_plain(admin_url()), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@dataclass
class Workspace:
    settings: Settings
    services: Services
    runs: RunService
    root: Path
    db_name: str
    project_id: str | None = None
    run_id: str | None = None

    @classmethod
    def create(cls, *, llm_provider=None, **overrides) -> "Workspace":
        root = Path(tempfile.mkdtemp(prefix="aiwb-eval-"))
        db_name = f"aiwb_t_{uuid.uuid4().hex[:12]}"
        url = create_database(db_name)
        base = {"environment": "eval", "storage.database_url": url, "storage.pool_size": 5, "storage.max_overflow": 5,
                "storage.object_store": "local", "storage.object_store_root": str(root / "objects"),
                "observability.log_level": "WARNING", "jobs.embedded_worker": False, "ingestion.chunk_bytes": 65536}
        settings = load_settings(overrides={**base, **overrides})
        services = build_services(settings, llm_provider=llm_provider)
        return cls(settings, services, RunService(services), root, db_name)

    def close(self) -> None:
        from graph.checkpointer import close_checkpointer
        close_checkpointer(self.runs.checkpointer)
        self.services.engine.dispose()
        shutil.rmtree(self.root, ignore_errors=True)
        drop_database(self.db_name)

    def project(self, name: str = "Golden estate", **kw) -> str:
        proj = self.runs.create_project(name=name, business_domain=kw.pop("business_domain", "Policy"),
                                        source_technologies=kw.pop("source_technologies", ["COBOL", "PL/SQL", "JCL"]),
                                        target_technologies=kw.pop("target_technologies", ["PHP"]),
                                        description=kw.pop("description", "evaluation estate"),
                                        user="golden-dataset:operator", **kw)
        self.project_id = proj.project_id
        return proj.project_id

    def upload_samples(self, project_id: str, source_name: str = "Capita Policy Admin",
                       target_name: str = "Integro") -> list[str]:
        ids = []
        for role, name in (("source", source_name), ("target", target_name)):
            data, filename = sample_bundle(role)
            up = self.runs.upload_bytes(project_id, data, filename, name, role, "golden-dataset:operator")
            assert up.status == "ready", (up.status, up.error)
            ids.append(up.upload_id)
        return ids

    def start_sample_run(self, source_name: str = "Legacy A", target_name: str = "Target B",
                         project_name: str | None = None, **project_kw) -> str:
        """Project + sample uploads + a run that stops at its first human gate (tests)."""
        pid = self.project(project_name or f"Sample {uuid.uuid4().hex[:6]}", **project_kw)
        return self.runs.start_run(pid, self.upload_samples(pid, source_name, target_name), "op", background=False)

    def run_golden_pipeline(self, decisions: dict[str, str] | None = None) -> str:
        decisions = {**GOLDEN_DECISIONS, **(decisions or {})}
        project_id = self.project_id or self.project()
        upload_ids = self.upload_samples(project_id)
        run_id = self.runs.start_run(project_id, upload_ids, "golden-dataset:operator", background=False)
        roles = {**GATE_ROLES, **{k: HumanRole(v) for k, v in self.settings.human_roles.items()}}
        for _ in range(12):
            status = self.runs.status(run_id)
            if status["status"] != "awaiting_human":
                break
            gate = status["current_gate"]
            for t in self.services.kc.list_tasks(run_id, status="pending", gate=gate):
                d = HumanDecision(item_id=t.item_id, item_type=t.item_type, decision=decisions[t.item_type],
                                  decided_by=f"golden-dataset:{gate}", role=roles[gate],
                                  rationale="reviewed golden decision")
                self.runs.decide_task(t.task_id, d, background=False)
        self.run_id = run_id
        return run_id


def fingerprint(settings: Settings) -> str:
    """Anything that changes behaviour changes this: config, prompts, naming and heuristic versions."""
    from reasoning.heuristics.anomaly import scoring as a
    from reasoning.heuristics.differential import severity as d
    from reasoning.heuristics.mapping import heuristics as mh
    from reasoning.heuristics import naming_match as nm
    parts = [settings.model_dump_json(exclude={"storage", "environment"}), NAMING_VERSION,
             *(f"{p.id}:{p.version}:{p.fingerprint}" for p in all_prompts()), mh.VERSION, nm.VERSION, a.VERSION, d.VERSION]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


class Timer:
    def __enter__(self) -> "Timer":
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *_: object) -> None:
        self.seconds = round(time.perf_counter() - self.t0, 3)
