"""Passwordless PostgreSQL in Azure: Microsoft Entra tokens from the Managed Identity.

With ``storage.entra_auth`` the database URL carries only the identity's role name; each new connection asks
the Managed Identity for an access token (cached until shortly before it expires) and presents it as the
password. Nothing secret is stored in configuration, Key Vault or the container.
"""
from __future__ import annotations

import threading
import time

SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
_lock = threading.Lock()
_cached: dict[str, float | str] = {}


def entra_token() -> str:
    with _lock:
        if _cached and float(_cached["expires_on"]) - time.time() > 300:
            return str(_cached["token"])
        from azure.identity import DefaultAzureCredential   # Managed Identity in Container Apps / AKS
        tok = DefaultAzureCredential().get_token(SCOPE)
        _cached.update(token=tok.token, expires_on=float(tok.expires_on))
        return tok.token


def psycopg_connection_class():
    """A psycopg Connection that injects a fresh token on every new pooled connection (LangGraph checkpointer)."""
    import psycopg

    class EntraConnection(psycopg.Connection):
        @classmethod
        def connect(cls, conninfo: str = "", **kwargs):  # type: ignore[override]
            kwargs["password"] = entra_token()
            return super().connect(conninfo, **kwargs)
    return EntraConnection
