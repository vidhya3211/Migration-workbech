"""Programmatic Alembic: bring a PostgreSQL database to the head revision.

Used at start-up when ``storage.auto_migrate`` is on (local, tests) and by ``python cli.py migrate`` (the
production release job). A PostgreSQL advisory lock serialises concurrent start-ups.
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
_LOCK_KEY = 7_242_019_001   # arbitrary, fixed: one migrator at a time per database


def alembic_config(engine: Engine) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%"))
    return cfg


def upgrade(engine: Engine, revision: str = "head") -> None:
    cfg = alembic_config(engine)
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _LOCK_KEY})
        try:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, revision)
            conn.commit()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            conn.commit()


def current(engine: Engine) -> str | None:
    with engine.connect() as conn:
        try:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        except Exception:
            return None
