"""Uploads and artefacts (§32). Raw content lives in the evidence store, never in graph state."""
from __future__ import annotations

from datetime import datetime

from pydantic import Field

from schemas.common import WorkbenchModel, SystemRef, utc_now


class UploadFile(WorkbenchModel):
    path: str             # posix path inside the bundle
    size_bytes: int
    sha256: str


class UploadManifest(WorkbenchModel):
    upload_id: str
    system: SystemRef
    filename: str
    bundle_sha256: str
    bundle_bytes: int
    files: list[UploadFile]
    received_at: datetime = Field(default_factory=utc_now)
    received_by: str

    @property
    def file_count(self) -> int:
        return len(self.files)


class Artefact(WorkbenchModel):
    artefact_id: str
    run_id: str
    upload_id: str
    system: SystemRef
    path: str                        # posix path inside the upload
    artefact_type: str               # registered in tools.parsers.registry
    language: str | None = None
    encoding: str | None = None
    sha256: str
    size_bytes: int
    line_count: int = 0
    confidence: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)
    route: str | None = None         # agent that should read it
    blob_uri: str                    # raw evidence store reference
    classification: str = "internal"  # internal | confidential (customer data)
    ingest: dict = Field(default_factory=dict)   # ingest facts: decoder, control totals, PII columns tokenised


class ArtefactAnalysis(WorkbenchModel):
    artefact_id: str
    agent: str
    status: str  # analysed | failed | deferred | unrouted | context_only
    summary: dict = Field(default_factory=dict)
    error: str | None = None
