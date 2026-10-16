"""Durable jobs: SKIP LOCKED claiming, lease expiry (a dead worker's job is re-claimed), retries, failures."""
from datetime import timedelta

from sqlalchemy import update

from db import models as m
from schemas.common import utc_now
from services.jobs import Worker


def test_jobs_are_claimed_once_and_reclaimed_after_a_dead_worker(ws):
    kc, cfg = ws.services.kc, ws.settings.jobs
    seen = []
    w = Worker(kc, cfg, {"echo": lambda p: seen.append(p["n"]) or {"n": p["n"]}}, name="w1")
    job_id = ws.runs.jobs.enqueue("echo", {"n": 1}, created_by="t")
    first = kc.claim_job("crashed-worker", cfg.lease_seconds)
    assert first.job_id == job_id and kc.claim_job("w2", cfg.lease_seconds) is None   # locked by the first claim
    # the claiming worker "dies": its lease lapses and the job is re-claimed and completed exactly once
    with kc.session() as s, s.begin():
        s.execute(update(m.Job).where(m.Job.job_id == job_id).values(locked_until=utc_now() - timedelta(seconds=1)))
    assert w.run_once() and seen == [1]
    job = kc.get_job(job_id)
    assert job.status == "succeeded" and job.attempts == 2 and job.result == {"n": 1}


def test_failures_are_explicit_and_retryable_errors_retry(ws):
    kc, cfg = ws.services.kc, ws.settings.jobs

    class Flaky(RuntimeError):
        retryable = True

    def boom(_):
        raise ValueError("bad input")

    def flaky(_):
        raise Flaky("transient")
    w = Worker(kc, cfg, {"boom": boom, "flaky": flaky})
    a = ws.runs.jobs.enqueue("boom", {}, created_by="t")
    b = ws.runs.jobs.enqueue("flaky", {}, created_by="t")
    w.drain()
    assert kc.get_job(a).status == "failed" and "bad input" in kc.get_job(a).error
    jb = kc.get_job(b)
    assert jb.status == "queued" and jb.attempts == 1 and jb.available_at > utc_now()   # backed off, will retry
