"""Bundle intake: one ZIP per system per upload, with required ``system_name`` / ``system_role``.

Bundles of several GB arrive in chunks (console -> API -> object store blocks) and are validated by a
background job, never inside an API request. Validation reads the zip's central directory through a seekable
stream (an HTTP range reader on Azure Blob Storage) and hashes every member as a stream: size, file count,
zip-slip, symlinks, zip-bomb ratio, duplicate paths and the per-file SHA-256 manifest the ingest agent
re-verifies later. Nothing about the system is hardcoded - the name and role are tags.
"""
from __future__ import annotations

import hashlib
import io
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

from config.settings import IngestionSettings


class UploadRejected(ValueError):
    """The bundle failed validation. Explicit - a run never starts on a bad bundle."""


@dataclass(frozen=True)
class MemberInfo:
    path: str
    info: zipfile.ZipInfo
    size_bytes: int


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    size_bytes: int
    sha256: str


def _safe_member(name: str) -> str:
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or any(part in ("..", "") for part in p.parts) or ":" in p.parts[0]:
        raise UploadRejected(f"unsafe path in bundle: {name!r}")
    return p.as_posix()


def skip_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return any(part.startswith(".") or part in ("__MACOSX", "__pycache__") for part in parts)


def open_zip(stream: BinaryIO) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(stream)
    except zipfile.BadZipFile as exc:
        raise UploadRejected(f"not a valid zip bundle: {exc}") from exc


def inspect_bundle(zf: zipfile.ZipFile, bundle_bytes: int, limits: IngestionSettings) -> list[MemberInfo]:
    """Structural validation from the central directory only (no member is decompressed)."""
    if bundle_bytes > limits.max_zip_bytes:
        raise UploadRejected(f"bundle is {bundle_bytes} bytes; limit {limits.max_zip_bytes}")
    members = [i for i in zf.infolist() if not i.is_dir()]
    if len(members) > limits.max_files:
        raise UploadRejected(f"bundle has {len(members)} files; limit {limits.max_files}")
    total = sum(i.file_size for i in members)
    if total > limits.max_uncompressed_bytes:
        raise UploadRejected(f"bundle expands to {total} bytes; limit {limits.max_uncompressed_bytes}")
    out: list[MemberInfo] = []
    seen: set[str] = set()
    for info in members:
        if stat.S_ISLNK(info.external_attr >> 16):
            raise UploadRejected(f"symlink in bundle: {info.filename!r}")
        path = _safe_member(info.filename)
        if skip_path(path):
            continue
        if info.compress_size and info.file_size / max(1, info.compress_size) > limits.max_compression_ratio:
            raise UploadRejected(f"suspicious compression ratio for {path!r}")
        if path.lower() in seen:
            raise UploadRejected(f"duplicate path in bundle: {path!r}")
        seen.add(path.lower())
        out.append(MemberInfo(path, info, info.file_size))
    if not out:
        raise UploadRejected("bundle contains no files")
    return out


class HashingReader(io.RawIOBase):
    """Wraps a member stream: hashes and counts every byte read; ``finish()`` drains and returns the digest."""

    def __init__(self, raw: BinaryIO, limit: int | None = None) -> None:
        self._raw, self._h, self.n, self._limit = raw, hashlib.sha256(), 0, limit

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        data = self._raw.read(len(b))
        if not data:
            return 0
        self.n += len(data)
        if self._limit is not None and self.n > self._limit:
            raise UploadRejected("member expands beyond its declared size")
        self._h.update(data)
        b[:len(data)] = data
        return len(data)

    def finish(self, chunk: int = 1024 * 1024) -> str:
        while self.read(chunk):
            pass
        return self._h.hexdigest()


def manifest(zf: zipfile.ZipFile, members: list[MemberInfo], chunk: int = 1024 * 1024) -> Iterator[ManifestEntry]:
    """Streams every member once to compute its SHA-256 (bounded memory whatever the bundle size)."""
    for mem in members:
        with zf.open(mem.info) as raw:
            hr = HashingReader(raw, limit=mem.size_bytes)
            digest = hr.finish(chunk)
        if hr.n != mem.size_bytes:
            raise UploadRejected(f"size mismatch extracting {mem.path!r}")
        yield ManifestEntry(mem.path, mem.size_bytes, digest)


def zip_directory(folder: Path) -> bytes:
    """Helper for samples/tests: bundle a folder exactly as the console upload would."""
    buf = io.BytesIO()
    write_zip(folder, buf)
    return buf.getvalue()


def write_zip(folder: Path, out: BinaryIO) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for p in sorted(folder.rglob("*")):
            if p.is_file() and not skip_path(p.relative_to(folder).as_posix()):
                zf.write(p, p.relative_to(folder).as_posix())
