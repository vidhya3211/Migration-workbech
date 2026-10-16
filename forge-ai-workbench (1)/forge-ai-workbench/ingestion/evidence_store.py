"""Raw evidence store: staged (PII-obfuscated) artefacts in object storage.

Agents read file content through this store by ``blob_uri``; raw content never travels in graph state,
logs or traces. Evidence keys are ``<project_id>/<run_id>/<role>/<path>`` in the evidence container, so one
project's evidence can be listed, retained or purged independently of another's.

Data extracts are additionally staged row-by-row in PostgreSQL (``staging.extracts``); agents and the
deterministic guard read customer rows from there, never by loading a whole extract into memory.
"""
from __future__ import annotations

import hashlib
import io
import threading
from collections import OrderedDict
from typing import BinaryIO

from ingestion.decode import BinaryContent, decode_bytes
from storage.objects import ObjectStore


class EvidenceStore:
    def __init__(self, objects: ObjectStore, container: str) -> None:
        self.objects = objects
        self.container = container

    def put(self, scope: str, rel_path: str, data: bytes) -> str:
        return self.objects.put_bytes(self.container, f"{scope}/{rel_path}", data)

    def put_stream(self, scope: str, rel_path: str, stream: BinaryIO) -> tuple[str, int, str]:
        """Streams into the store; returns (uri, size, sha256)."""
        return self.objects.put_stream(self.container, f"{scope}/{rel_path}", stream)

    def open(self, uri: str) -> BinaryIO:
        return self.objects.open(uri)

    def get_bytes(self, uri: str) -> bytes:
        return self.objects.get_bytes(uri)

    def size(self, uri: str) -> int:
        return self.objects.size(uri)


class EvidenceReader:
    """Decoded text access for code/document agents and the deterministic guard.

    Text is cached (bounded by total characters) because the guard re-reads the files the extractors just
    parsed; anything larger than ``max_cached_chars`` is decoded on demand and never held."""

    def __init__(self, store: EvidenceStore, max_cached_chars: int = 64 * 1024 * 1024,
                 max_file_chars: int = 8 * 1024 * 1024) -> None:
        self.store = store
        self._cache: OrderedDict[str, tuple[str | None, str | None]] = OrderedDict()
        self._chars = 0
        self._max_total, self._max_file = max_cached_chars, max_file_chars
        self._lock = threading.Lock()

    def _decode(self, uri: str) -> tuple[str | None, str | None]:
        with self._lock:
            hit = self._cache.get(uri)
            if hit is not None:
                self._cache.move_to_end(uri)
                return hit
        data = self.store.get_bytes(uri)
        try:
            value: tuple[str | None, str | None] = decode_bytes(data)
        except BinaryContent:
            value = (None, None)
        size = len(value[0] or "")
        if size <= self._max_file:
            with self._lock:
                self._cache[uri] = value
                self._chars += size
                while self._chars > self._max_total and self._cache:
                    _, old = self._cache.popitem(last=False)
                    self._chars -= len(old[0] or "")
        return value

    def text(self, uri: str) -> str | None:
        return self._decode(uri)[0]

    def encoding(self, uri: str) -> str | None:
        return self._decode(uri)[1]

    def lines(self, uri: str) -> list[str]:
        text = self.text(uri)
        return text.split("\n") if text is not None else []

    def bytes(self, uri: str) -> bytes:
        return self.store.get_bytes(uri)

    def head(self, uri: str, n: int = 65536) -> bytes:
        with self.store.open(uri) as f:
            return f.read(n)

    def open(self, uri: str) -> BinaryIO:
        return self.store.open(uri)

    def sha256(self, uri: str) -> str:
        h = hashlib.sha256()
        with self.store.open(uri) as f:
            for buf in iter(lambda: f.read(1024 * 1024), b""):
                h.update(buf)
        return h.hexdigest()


class MemoryEvidenceStore:
    """In-memory store for self-tests (no I/O)."""

    def __init__(self, data: dict[str, bytes] | None = None) -> None:
        self.data = dict(data or {})

    def get_bytes(self, uri: str) -> bytes:
        return self.data[uri]

    def open(self, uri: str) -> BinaryIO:
        return io.BytesIO(self.data[uri])

    def size(self, uri: str) -> int:
        return len(self.data[uri])
