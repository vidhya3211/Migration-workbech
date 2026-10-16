"""Durable background jobs: upload validation, run start, run resume.

The job table in PostgreSQL is the source of truth. Workers claim jobs with ``FOR UPDATE SKIP LOCKED`` and hold
a lease they renew while working; if a worker dies its lease expires and another worker re-claims the job (a
run restarts from its last LangGraph checkpoint, so no completed step is repeated).

Backends:
* ``postgres``    - workers poll the job table (any environment).
* ``service_bus`` - Azure Service Bus carries the job id (Managed Identity); Container Apps scales the worker
                    on queue length with KEDA. The job row is still claimed in PostgreSQL, so a duplicate or
                    replayed message can never run a job twice.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import traceback
from typing import Callable

from config.settings import JobSettings
from db.repository import KnowledgeCore
from observability.logging.logger import Event, log_event
from schemas.common import new_id

Handler = Callable[[dict], dict | None]


class JobQueue:
    def __init__(self, kc: KnowledgeCore, settings: JobSettings) -> None:
        self.kc = kc
        self.settings = settings
        self._sender = None

    def enqueue(self, kind: str, payload: dict, *, created_by: str, project_id: str | None = None,
                run_id: str | None = None, upload_id: str | None = None) -> str:
        job_id = new_id("JOB")
        self.kc.enqueue_job(job_id=job_id, kind=kind, payload=payload, created_by=created_by, project_id=project_id,
                            run_id=run_id, upload_id=upload_id, max_attempts=self.settings.max_attempts)
        if self.settings.backend == "service_bus":
            self._send(job_id)
        log_event(Event.RUN_STARTED, f"job {kind} queued", job_id=job_id, run_id=run_id)
        return job_id

    def _send(self, job_id: str) -> None:  # pragma: no cover - requires Azure Service Bus
        from azure.identity import DefaultAzureCredential
        from azure.servicebus import ServiceBusClient, ServiceBusMessage
        if self._sender is None:
            client = ServiceBusClient(self.settings.service_bus_namespace, credential=DefaultAzureCredential())
            self._sender = client.get_queue_sender(self.settings.service_bus_queue)
        self._sender.send_messages(ServiceBusMessage(json.dumps({"job_id": job_id}), message_id=job_id))


class Worker:
    """Executes jobs. ``run_forever`` in a worker process (``python cli.py worker``) or as embedded threads."""

    def __init__(self, kc: KnowledgeCore, settings: JobSettings, handlers: dict[str, Handler],
                 name: str | None = None) -> None:
        self.kc = kc
        self.settings = settings
        self.handlers = handlers
        self.name = name or f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # ---------------------------------------------------------------- one job
    def run_job(self, job) -> None:
        handler = self.handlers.get(job.kind)
        if handler is None:
            self.kc.finish_job(job.job_id, ok=False, error=f"no handler for job kind {job.kind!r}")
            return
        beat = threading.Event()

        def heartbeat() -> None:
            while not beat.wait(self.settings.heartbeat_seconds):
                self.kc.heartbeat_job(job.job_id, self.name, self.settings.lease_seconds)
        t = threading.Thread(target=heartbeat, name=f"hb-{job.job_id}", daemon=True)
        t.start()
        try:
            result = handler(dict(job.payload, job_id=job.job_id, attempt=job.attempts)) or {}
            self.kc.finish_job(job.job_id, ok=True, result=result)
        except Exception as exc:   # explicit failure - visible on the job and in the logs, never swallowed
            retry = getattr(exc, "retryable", False)
            self.kc.finish_job(job.job_id, ok=False, error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-2000:]}",
                               retry_in=10.0 * job.attempts if retry else None)
            log_event(Event.RUN_FAILED, f"job {job.job_id} failed: {exc}", job_id=job.job_id)
        finally:
            beat.set()

    def run_once(self, job_id: str | None = None) -> bool:
        job = self.kc.claim_job(self.name, self.settings.lease_seconds, job_id=job_id)
        if job is None:
            return False
        self.run_job(job)
        return True

    def drain(self, max_jobs: int = 1000) -> int:
        """Run queued jobs inline until none are left (tests, CLI)."""
        n = 0
        while n < max_jobs and self.run_once():
            n += 1
        return n

    # ---------------------------------------------------------------- loops
    def run_forever(self) -> None:
        if self.settings.backend == "service_bus":
            self._service_bus_loop()
            return
        while not self._stop.is_set():
            try:
                if not self.run_once():
                    self._stop.wait(self.settings.poll_seconds)
            except Exception as exc:  # a database blip must not kill the worker
                log_event(Event.RUN_FAILED, f"worker loop error: {exc}")
                self._stop.wait(self.settings.poll_seconds * 5)

    def _service_bus_loop(self) -> None:  # pragma: no cover - requires Azure Service Bus
        from azure.identity import DefaultAzureCredential
        from azure.servicebus import ServiceBusClient
        client = ServiceBusClient(self.settings.service_bus_namespace, credential=DefaultAzureCredential())
        with client.get_queue_receiver(self.settings.service_bus_queue, max_wait_time=30) as receiver:
            while not self._stop.is_set():
                for msg in receiver.receive_messages(max_message_count=1, max_wait_time=30):
                    try:
                        job_id = json.loads(str(msg)).get("job_id")
                        self.run_once(job_id)
                        receiver.complete_message(msg)
                    except Exception:
                        receiver.abandon_message(msg)
                # stale leases (a worker died) are re-claimed even without a new message
                self.run_once()


def start_embedded_workers(kc: KnowledgeCore, settings: JobSettings, handlers: dict[str, Handler]) -> list[Worker]:
    workers = []
    for i in range(max(1, settings.worker_concurrency)):
        w = Worker(kc, settings, handlers, name=f"embedded-{os.getpid()}-{i}")
        threading.Thread(target=w.run_forever, name=f"aiwb-worker-{i}", daemon=True).start()
        workers.append(w)
        time.sleep(0.01)
    return workers
