"""Durable LangGraph checkpointer on PostgreSQL (the same database as the knowledge core).

Human decisions may take days: a run waiting at a gate is a checkpoint row, not a blocked thread, so it
survives restarts, redeployments and worker failover, and never blocks other runs. A connection pool lets
concurrent runs and resumes checkpoint in parallel.
"""
from __future__ import annotations

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config.settings import Settings


def build_checkpointer(settings: Settings, *, max_size: int = 10) -> PostgresSaver:
    extra = {}
    if settings.storage.entra_auth:
        from db.credentials import psycopg_connection_class
        extra["connection_class"] = psycopg_connection_class()
    pool = ConnectionPool(settings.psycopg_dsn(), min_size=1, max_size=max_size, open=True,
                          kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}, **extra)
    saver = PostgresSaver(pool)
    saver.setup()            # idempotent: creates / migrates LangGraph's checkpoint tables
    return saver


def close_checkpointer(saver: PostgresSaver) -> None:
    conn = getattr(saver, "conn", None)
    if isinstance(conn, ConnectionPool):
        conn.close()
