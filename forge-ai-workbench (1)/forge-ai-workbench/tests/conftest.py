from __future__ import annotations

import os
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")
# administrator sign-in for the API tests (development-only clear password; production uses the hash from Key Vault)
os.environ.setdefault("AIWB_ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("AIWB_AUTH_SIGNING_KEY", "test-signing-key-0123456789-abcdefghijklmnop")
os.environ.setdefault("AIWB_PII_HMAC_KEY", "test-pii-key")

from evals.harness import Workspace  # noqa: E402


@pytest.fixture(scope="session")
def golden() -> Workspace:
    """One full golden pipeline run shared by the integration tests (the real graph, real gates, PostgreSQL)."""
    ws = Workspace.create()
    ws.run_golden_pipeline()
    yield ws
    ws.close()


@pytest.fixture()
def ws() -> Workspace:
    w = Workspace.create()
    yield w
    w.close()


@pytest.fixture()
def samples() -> Path:
    return Path(__file__).resolve().parent.parent / "samples"
