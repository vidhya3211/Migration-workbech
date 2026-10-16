"""Typed platform configuration.

``config/workbench.yaml`` holds platform defaults; ``AIWB__SECTION__KEY`` environment variables override them
(``__`` separates nesting levels). Secrets (database DSN with password, LLM keys, the admin password hash, the
token-signing key) are read from the environment only - in Azure they are injected from Key Vault via Managed
Identity - and are never written back into the settings object that gets logged.

Projects layer their own overrides on top of the platform settings (``effective_settings``): each migration
project can bring its own business vocabulary, heuristics, root-cause catalogue, PII columns and pipeline
without a code change. Only the sections in ``PROJECT_OVERRIDABLE`` may be overridden by a project.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = Path(__file__).with_name("workbench.yaml")
ENV_PREFIX = "AIWB__"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StorageSettings(_Section):
    """PostgreSQL is the only database (knowledge core, extract staging, job queue and LangGraph checkpoints).

    Raw bundles, staged evidence and Headroom originals live in the object store: Azure Blob Storage (ADLS
    Gen2) in Azure, a local directory with the same layout for development."""
    database_url: str                                   # postgresql+psycopg://... (AIWB_DATABASE_URL overrides)
    pool_size: int = 10
    max_overflow: int = 20
    statement_timeout_ms: int = 0                       # 0 = server default
    auto_migrate: bool = True                           # local/test: alembic upgrade at start; production: release job
    entra_auth: bool = False                            # Azure: Managed Identity token as the password (no secret)
    object_store: Literal["local", "azure_blob"] = "local"
    object_store_root: str = "./var/objects"           # local backend only
    blob_account_url: str = ""                          # https://<account>.blob.core.windows.net (Managed Identity)
    uploads_container: str = "uploads"
    evidence_container: str = "evidence"
    context_container: str = "context"

    @field_validator("database_url")
    @classmethod
    def _postgres_only(cls, v: str) -> str:
        if not v.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("the AI Workbench runs on PostgreSQL only: database_url must be postgresql+psycopg://...")
        return v


class RetrievalSettings(_Section):
    """Certified retrieval runs on PostgreSQL: full-text search always, pgvector semantic ranking when the
    ``vector`` extension is present (Azure Database for PostgreSQL) and an embedding deployment is configured."""
    semantic: bool = False
    embedding_provider: Literal["azure_openai", "openai"] = "azure_openai"
    embedding_endpoint: str = ""
    embedding_deployment: str = ""
    embedding_api_version: str = "2024-10-21"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: Literal[1536] = 1536
    min_similarity: float = 0.25
    min_text_rank: float = 0.01
    allow_external_embeddings: bool = False


class PiiSettings(_Section):
    obfuscate_columns: list[str] = Field(default_factory=list)
    classification_columns: list[str] = Field(default_factory=list)


class IngestionSettings(_Section):
    max_zip_bytes: int
    max_files: int
    max_uncompressed_bytes: int
    max_compression_ratio: int
    chunk_bytes: int = 16 * 1024 * 1024                  # browser -> API -> object store block size
    stream_buffer_bytes: int = 1024 * 1024
    copy_batch_rows: int = 50_000                        # PostgreSQL COPY batch when staging data extracts
    csv_delimiters: list[str] = Field(default_factory=lambda: [",", "|", "\t", ";"])
    pii: PiiSettings


class ScaleSettings(_Section):
    """Bounds that keep memory flat whatever the upload size (2-4 GB bundles, tens of millions of rows)."""
    fanout_max_concurrency: int = 8                      # parallel extraction tasks per run
    bulk_batch_size: int = 2_000                         # governed bulk writes per transaction
    read_batch_rows: int = 10_000                        # server-side cursor fetch size
    profile_sample_rows: int = 50_000                    # type inference / pattern sample per column
    mapping_profile_sample: int = 5_000
    validation_distinct_cap: int = 1_000_000             # distinct values dry-run per mapping before sampling
    entity_match_max_block: int = 50                     # blocking: larger blocks are reported, not paired
    cohort_record_refs: int = 200
    cohort_evidence: int = 20
    lineage_breaks_per_cluster: int = 50
    gate_items_page: int = 500                           # HITL items listed per page


class LLMModelSettings(_Section):
    provider: str
    deployment: str = ""
    endpoint: str = ""
    api_version: str = ""
    temperature: float | None = 0.0          # None = provider default (some newer models reject the parameter)
    max_input_tokens: int = 32000
    max_output_tokens: int = 2048
    input_cost_per_1k: Decimal = Decimal("0")
    output_cost_per_1k: Decimal = Decimal("0")
    cached_cost_per_1k: Decimal = Decimal("0")


class LLMSettings(_Section):
    provider: str
    default_model: str
    models: dict[str, LLMModelSettings]
    timeout_seconds: float = 60
    allow_external_providers: bool = False   # public OpenAI/Anthropic/Gemini endpoints in production
    max_retries: int = 2
    structured_output_retries: int = 1


class HeadroomSettings(_Section):
    mode: Literal["library", "proxy", "disabled"] = "library"
    proxy_base_url: str = ""
    kompress_model: str = "disabled"
    min_tokens_to_compress: int = 250
    target_ratio: float = 0.5
    production_optimization_enabled: bool = False


class BudgetSettings(_Section):
    llm_calls: int
    input_tokens: int
    output_tokens: int
    mcp_calls: int
    tool_calls: int
    ats_branches: int
    ats_depth: int
    execution_seconds: float
    estimated_cost: Decimal


class ATSSettings(_Section):
    max_depth: int
    max_branches: int
    max_children: int
    token_budget: int
    time_budget_seconds: float
    model_call_budget: int
    confidence_threshold: float
    min_evidence: int


class MCPToolSettings(_Section):
    allowed_agents: list[str]
    data_classification: Literal["public", "internal", "confidential", "restricted"] = "internal"
    rate_limit_per_minute: int = 60
    timeout_seconds: float = 10
    requires_human_approval: bool = False


class MCPServerSettings(_Section):
    transport: Literal["inprocess", "stdio", "http"]
    enabled: bool = True
    auth: Literal["none", "managed_identity", "bearer"] = "none"
    command: list[str] = Field(default_factory=list)
    url: str = ""
    tools: dict[str, MCPToolSettings]


class MCPSettings(_Section):
    servers: dict[str, MCPServerSettings] = Field(default_factory=dict)


class GuardrailSettings(_Section):
    max_prompt_chars: int
    block_on_pii_to_llm: bool = True
    injection_block_threshold: float = 0.6


class AuthSettings(_Section):
    """Administrator sign-in only. ``admin``: a single administrator account whose password hash
    (``AIWB_ADMIN_PASSWORD_HASH``) and token-signing key (``AIWB_AUTH_SIGNING_KEY``) come from Key Vault.
    ``entra``: Microsoft Entra ID tokens that carry the ``admin_app_role`` app role."""
    mode: Literal["admin", "entra"] = "admin"
    admin_username: str = "admin"
    token_ttl_minutes: int = 480
    max_failed_logins: int = 5
    lockout_seconds: int = 300
    entra_tenant_id: str = ""
    entra_audience: str = ""
    role_claim: str = "roles"
    admin_app_role: str = "Workbench.Admin"


class JobSettings(_Section):
    """Background work (upload validation, runs, resumes) never runs in an API request.

    ``postgres``: durable job table claimed with ``FOR UPDATE SKIP LOCKED`` (works everywhere PostgreSQL does).
    ``service_bus``: Azure Service Bus queue carries the job id; the job table stays the source of truth and
    Container Apps scales workers on queue length (KEDA)."""
    backend: Literal["postgres", "service_bus"] = "postgres"
    embedded_worker: bool = True                         # local: the API process also runs a worker thread
    worker_concurrency: int = 2
    poll_seconds: float = 1.0
    lease_seconds: int = 120
    heartbeat_seconds: int = 30
    max_attempts: int = 3
    service_bus_namespace: str = ""                      # <namespace>.servicebus.windows.net (Managed Identity)
    service_bus_queue: str = "aiwb-jobs"


class ApiSettings(_Section):
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])
    default_page_size: int = 200
    max_page_size: int = 1000
    max_chunk_bytes: int = 64 * 1024 * 1024


class BrandingSettings(_Section):
    organisation_name: str = ""
    product_name: str = "Migration Workbench"
    business_domains: list[str] = Field(default_factory=lambda: ["Policy", "Claims", "Finance", "Customer",
                                                                 "Distribution", "Other"])


class ObservabilitySettings(_Section):
    otel_enabled: bool = False
    service_name: str = "ai-workbench"
    log_level: str = "INFO"
    log_payloads: bool = False


class RootCause(_Section):
    code: str
    description: str
    signatures: list[str]


class Settings(_Section):
    environment: Literal["local", "eval", "test", "production"]
    storage: StorageSettings
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    ingestion: IngestionSettings
    scale: ScaleSettings = Field(default_factory=ScaleSettings)
    llm: LLMSettings
    headroom: HeadroomSettings
    budgets: BudgetSettings
    ats: ATSSettings
    heuristics: dict[str, Any]
    synonyms: dict[str, list[str]]
    token_synonyms: dict[str, str] = Field(default_factory=dict)
    abbreviations: dict[str, str]
    domains: dict[str, str]
    root_causes: list[RootCause]
    mcp: MCPSettings
    guardrails: GuardrailSettings
    human_roles: dict[str, str]
    eval_gate: dict[str, float]
    auth: AuthSettings
    jobs: JobSettings = Field(default_factory=JobSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    branding: BrandingSettings = Field(default_factory=BrandingSettings)
    pipeline: str = "default"                            # pipeline definition in config/pipelines/<name>.yaml
    observability: ObservabilitySettings

    def resolve_path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()

    def database_url(self) -> str:
        return secret("AIWB_DATABASE_URL") or self.storage.database_url

    def psycopg_dsn(self) -> str:
        """The same database as a plain libpq DSN (LangGraph checkpointer, COPY)."""
        return self.database_url().replace("postgresql+psycopg://", "postgresql://", 1)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json(exclude={"storage"}).encode("utf-8")).hexdigest()[:16]


# sections a project may override (business vocabulary and judgement thresholds - never storage, auth or budgets
# that protect the platform)
PROJECT_OVERRIDABLE = frozenset({"heuristics", "synonyms", "token_synonyms", "abbreviations", "domains",
                                 "root_causes", "pii", "scale", "ats", "eval_gate", "human_roles", "pipeline"})


def _apply_env_overrides(data: dict[str, Any], environ: dict[str, str]) -> None:
    for key, value in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = [p.lower() for p in key[len(ENV_PREFIX):].split("__")]
        node = data
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = yaml.safe_load(value)


def _set_dotted(data: dict[str, Any], dotted: str, value: Any) -> None:
    node = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def load_settings(path: Path | None = None, overrides: dict[str, Any] | None = None,
                  environ: dict[str, str] | None = None) -> Settings:
    data = yaml.safe_load((path or DEFAULT_CONFIG).read_text(encoding="utf-8"))
    _apply_env_overrides(data, dict(os.environ) if environ is None else environ)
    for dotted, value in (overrides or {}).items():
        _set_dotted(data, dotted, value)
    return Settings.model_validate(data)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def _deep_merge(base: Any, extra: Any) -> Any:
    if isinstance(base, dict) and isinstance(extra, dict):
        out = dict(base)
        for k, v in extra.items():
            out[k] = _deep_merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(extra)


class InvalidProjectSettings(ValueError):
    pass


def effective_settings(base: Settings, project_overrides: dict[str, Any] | None) -> Settings:
    """Platform settings with a project's overrides layered on top (validated; unknown sections refused)."""
    if not project_overrides:
        return base
    bad = set(project_overrides) - PROJECT_OVERRIDABLE
    if bad:
        raise InvalidProjectSettings(f"a project may not override: {sorted(bad)}")
    data = base.model_dump(mode="json")
    for key, value in project_overrides.items():
        if key == "pii":
            data["ingestion"]["pii"] = _deep_merge(data["ingestion"]["pii"], value)
        else:
            data[key] = _deep_merge(data.get(key), value)
    try:
        return Settings.model_validate(data)
    except Exception as exc:  # pydantic ValidationError -> a clear 422 for the API
        raise InvalidProjectSettings(str(exc)) from exc


# ------------------------------------------------------------------ the settings active for the current work
_active: ContextVar[Settings | None] = ContextVar("workbench_active_settings", default=None)


def active_settings() -> Settings:
    """The settings of the project whose work is executing (falls back to the platform settings)."""
    return _active.get() or get_settings()


@contextmanager
def use_settings(settings: Settings) -> Iterator[Settings]:
    token = _active.set(settings)
    try:
        yield settings
    finally:
        _active.reset(token)


def settings_digest(settings: Settings) -> str:
    """Cache key for anything derived from the vocabulary sections."""
    return hashlib.sha256(json.dumps([settings.synonyms, settings.abbreviations, settings.token_synonyms],
                                     sort_keys=True).encode("utf-8")).hexdigest()[:16]


def secret(name: str) -> str | None:
    """Secrets come only from the process environment (populated from Key Vault in Azure)."""
    return os.environ.get(name) or None
