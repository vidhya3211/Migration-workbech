"""PostgreSQL engine and session factory (the only database the AI Workbench runs on).

Azure Database for PostgreSQL Flexible Server in Azure; the docker-compose ``postgres`` service locally.
Schema changes are Alembic migrations (``db/migrations``): ``storage.auto_migrate`` applies them at start-up
locally and in tests; in production they run once as a release job before new replicas start.
"""
from __future__ import annotations

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import sessionmaker

from config.settings import Settings


def make_engine(settings: Settings) -> Engine:
    url = settings.database_url()
    if not url.startswith("postgresql"):
        raise ValueError("the AI Workbench runs on PostgreSQL only")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    st = settings.storage
    engine = create_engine(url, pool_pre_ping=True, pool_size=st.pool_size, max_overflow=st.max_overflow,
                           pool_recycle=1800, future=True)
    if st.entra_auth:
        from db.credentials import entra_token

        @event.listens_for(engine, "do_connect")
        def _token(dialect, conn_rec, cargs, cparams):  # noqa: ANN001
            cparams["password"] = entra_token()
    if st.statement_timeout_ms:
        @event.listens_for(engine, "connect")
        def _timeout(dbapi_conn, _):  # noqa: ANN001
            with dbapi_conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {int(st.statement_timeout_ms)}")
    return engine


def make_session_factory(engine: Engine, *, migrate: bool = True) -> sessionmaker:
    if migrate:
        from db.migrate import upgrade
        upgrade(engine)
    return sessionmaker(engine, expire_on_commit=False)
