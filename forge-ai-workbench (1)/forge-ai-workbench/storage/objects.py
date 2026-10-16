"""Object storage for raw bundles, staged evidence and Headroom originals.

One interface, two backends with the same ``obj://<container>/<key>`` URIs:

* ``AzureBlobObjectStore`` - Azure Blob Storage / ADLS Gen2 through Managed Identity (no keys in config).
  Uploads are block blobs (``stage_block`` per console chunk, ``commit_block_list`` on completion); reads are
  HTTP range requests behind a seekable file object, so a multi-GB zip is inspected without downloading it.
* ``LocalObjectStore`` - a directory with the same layout, for development and tests.

Nothing here buffers a whole object in memory: writes stream in ``chunk`` sized pieces and reads are seekable
streams. Raw content never travels in graph state, logs or traces - only ``obj://`` references do.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import threading
from pathlib import Path
from typing import BinaryIO, Iterator, Protocol

SCHEME = "obj://"
_KEY = re.compile(r"^[A-Za-z0-9._\-/ ()+&,=@!#$%~']+$")


class ObjectNotFound(FileNotFoundError):
    pass


def make_uri(container: str, key: str) -> str:
    return f"{SCHEME}{container}/{key}"


def parse_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith(SCHEME):
        raise ValueError(f"not an object uri: {uri!r}")
    container, _, key = uri[len(SCHEME):].partition("/")
    if not container or not key:
        raise ValueError(f"malformed object uri: {uri!r}")
    return container, key


def _check_key(key: str) -> str:
    k = key.replace("\\", "/")
    if k.startswith("/") or any(p in ("..", "") for p in k.split("/")) or not _KEY.match(k):
        raise ValueError(f"unsafe object key: {key!r}")
    return k


class ObjectStore(Protocol):
    backend: str

    def put_bytes(self, container: str, key: str, data: bytes) -> str: ...
    def put_stream(self, container: str, key: str, stream: BinaryIO) -> tuple[str, int, str]: ...
    def open(self, uri: str) -> BinaryIO: ...
    def get_bytes(self, uri: str) -> bytes: ...
    def size(self, uri: str) -> int: ...
    def exists(self, uri: str) -> bool: ...
    def delete(self, uri: str) -> None: ...
    def stage_part(self, container: str, key: str, index: int, data: bytes) -> None: ...
    def commit_parts(self, container: str, key: str, count: int) -> tuple[str, int]: ...


def copy_stream(src: BinaryIO, dst: BinaryIO, chunk: int = 1024 * 1024) -> tuple[int, str]:
    h, n = hashlib.sha256(), 0
    while True:
        buf = src.read(chunk)
        if not buf:
            break
        h.update(buf)
        dst.write(buf)
        n += len(buf)
    return n, h.hexdigest()


def sha256_stream(stream: BinaryIO, chunk: int = 1024 * 1024) -> tuple[int, str]:
    h, n = hashlib.sha256(), 0
    while True:
        buf = stream.read(chunk)
        if not buf:
            break
        h.update(buf)
        n += len(buf)
    return n, h.hexdigest()


# ====================================================================================== local
class LocalObjectStore:
    backend = "local"

    def __init__(self, root: Path, chunk: int = 1024 * 1024) -> None:
        root = Path(root).resolve()
        if os.name == "nt" and not str(root).startswith("\\\\?\\"):
            # extended-length paths: deep bundle layouts exceed the 260-character Windows limit
            root = Path("\\\\?\\" + str(root))
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.chunk = chunk
        self._lock = threading.Lock()

    def _path(self, container: str, key: str) -> Path:
        p = (self.root / _check_key(container) / _check_key(key)).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("object key escapes the store root")
        return p

    def _from_uri(self, uri: str) -> Path:
        return self._path(*parse_uri(uri))

    def put_bytes(self, container: str, key: str, data: bytes) -> str:
        return self.put_stream(container, key, io.BytesIO(data))[0]

    def put_stream(self, container: str, key: str, stream: BinaryIO) -> tuple[str, int, str]:
        path = self._path(container, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".part-{os.getpid()}-{threading.get_ident()}")
        with open(tmp, "wb") as out:
            n, digest = copy_stream(stream, out, self.chunk)
        os.replace(tmp, path)
        return make_uri(container, key), n, digest

    def open(self, uri: str) -> BinaryIO:
        path = self._from_uri(uri)
        if not path.exists():
            raise ObjectNotFound(uri)
        return open(path, "rb")

    def get_bytes(self, uri: str) -> bytes:
        with self.open(uri) as f:
            return f.read()

    def size(self, uri: str) -> int:
        path = self._from_uri(uri)
        if not path.exists():
            raise ObjectNotFound(uri)
        return path.stat().st_size

    def exists(self, uri: str) -> bool:
        return self._from_uri(uri).exists()

    def delete(self, uri: str) -> None:
        self._from_uri(uri).unlink(missing_ok=True)
        parts = self._parts_dir(*parse_uri(uri))
        if parts.exists():
            shutil.rmtree(parts, ignore_errors=True)

    # ---- multipart (console chunked upload)
    def _parts_dir(self, container: str, key: str) -> Path:
        p = self._path(container, key)
        return p.with_name(p.name + ".parts")

    def stage_part(self, container: str, key: str, index: int, data: bytes) -> None:
        d = self._parts_dir(container, key)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{index:08d}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, d / f"{index:08d}")

    def staged_parts(self, container: str, key: str) -> list[int]:
        d = self._parts_dir(container, key)
        return sorted(int(p.name) for p in d.iterdir() if p.name.isdigit()) if d.exists() else []

    def commit_parts(self, container: str, key: str, count: int) -> tuple[str, int]:
        d = self._parts_dir(container, key)
        missing = [i for i in range(count) if not (d / f"{i:08d}").exists()]
        if missing:
            raise ValueError(f"upload incomplete: {len(missing)} chunk(s) missing (first {missing[:5]})")
        path = self._path(container, key)
        total = 0
        with open(path, "wb") as out:
            for i in range(count):
                with open(d / f"{i:08d}", "rb") as part:
                    total += copy_stream(part, out, self.chunk)[0]
        shutil.rmtree(d, ignore_errors=True)
        return make_uri(container, key), total


# ====================================================================================== azure
class BlobRangeReader(io.RawIOBase):
    """Seekable, read-only view of a blob using HTTP range requests (zipfile reads only what it needs)."""

    def __init__(self, blob_client, size: int, block: int = 4 * 1024 * 1024) -> None:
        self._blob, self._size, self._pos = blob_client, size, 0
        self._block = block
        self._buf_start, self._buf = -1, b""

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, min(self._size, base + offset))
        return self._pos

    def readinto(self, b) -> int:
        if self._pos >= self._size:
            return 0
        want = min(len(b), self._size - self._pos)
        if not (self._buf_start <= self._pos < self._buf_start + len(self._buf)):
            length = min(max(want, self._block), self._size - self._pos)
            self._buf = self._blob.download_blob(offset=self._pos, length=length).readall()
            self._buf_start = self._pos
        off = self._pos - self._buf_start
        chunk = self._buf[off:off + want]
        b[:len(chunk)] = chunk
        self._pos += len(chunk)
        return len(chunk)


class AzureBlobObjectStore:
    backend = "azure_blob"

    def __init__(self, account_url: str, chunk: int = 4 * 1024 * 1024, containers: tuple[str, ...] = ()) -> None:
        """Managed Identity against ``account_url`` in Azure. ``AIWB_BLOB_CONNECTION_STRING`` (a secret, for local
        development against the Azurite emulator only) replaces it when set."""
        from azure.storage.blob import BlobServiceClient
        from config.settings import secret
        conn = secret("AIWB_BLOB_CONNECTION_STRING")
        if conn:
            self._svc = BlobServiceClient.from_connection_string(conn)
        else:
            from azure.identity import DefaultAzureCredential
            if not account_url:
                raise ValueError("storage.blob_account_url is required for the azure_blob object store")
            self._svc = BlobServiceClient(account_url, credential=DefaultAzureCredential())
        self.chunk = chunk
        for c in containers:          # created by the infrastructure in Azure; created here for a fresh emulator
            try:
                self._svc.create_container(_check_key(c))
            except Exception:         # already exists, or not permitted: the infrastructure owns it
                pass

    def _blob(self, container: str, key: str):
        return self._svc.get_blob_client(_check_key(container), _check_key(key))

    def _from_uri(self, uri: str):
        return self._blob(*parse_uri(uri))

    def put_bytes(self, container: str, key: str, data: bytes) -> str:
        self._blob(container, key).upload_blob(data, overwrite=True)
        return make_uri(container, key)

    def put_stream(self, container: str, key: str, stream: BinaryIO) -> tuple[str, int, str]:
        blob = self._blob(container, key)
        h, n, ids = hashlib.sha256(), 0, []
        from azure.storage.blob import BlobBlock
        i = 0
        while True:
            buf = stream.read(self.chunk)
            if not buf:
                break
            h.update(buf)
            n += len(buf)
            block_id = f"{i:08d}".encode().hex()
            blob.stage_block(block_id, buf)
            ids.append(BlobBlock(block_id=block_id))
            i += 1
        blob.commit_block_list(ids)
        return make_uri(container, key), n, h.hexdigest()

    def open(self, uri: str) -> BinaryIO:
        from azure.core.exceptions import ResourceNotFoundError
        blob = self._from_uri(uri)
        try:
            size = blob.get_blob_properties().size
        except ResourceNotFoundError as exc:
            raise ObjectNotFound(uri) from exc
        return io.BufferedReader(BlobRangeReader(blob, size), buffer_size=self.chunk)

    def get_bytes(self, uri: str) -> bytes:
        return self._from_uri(uri).download_blob().readall()

    def size(self, uri: str) -> int:
        return self._from_uri(uri).get_blob_properties().size

    def exists(self, uri: str) -> bool:
        return self._from_uri(uri).exists()

    def delete(self, uri: str) -> None:
        self._from_uri(uri).delete_blob(delete_snapshots="include")

    def stage_part(self, container: str, key: str, index: int, data: bytes) -> None:
        self._blob(container, key).stage_block(f"{index:08d}".encode().hex(), data)

    def commit_parts(self, container: str, key: str, count: int) -> tuple[str, int]:
        from azure.storage.blob import BlobBlock
        blob = self._blob(container, key)
        staged = {b.id for b in blob.get_block_list("uncommitted")[1]}
        ids = [f"{i:08d}".encode().hex() for i in range(count)]
        missing = [i for i, b in enumerate(ids) if b not in staged]
        if missing:
            raise ValueError(f"upload incomplete: {len(missing)} chunk(s) missing (first {missing[:5]})")
        blob.commit_block_list([BlobBlock(block_id=b) for b in ids])
        return make_uri(container, key), blob.get_blob_properties().size


def build_object_store(settings) -> ObjectStore:
    st = settings.storage
    if st.object_store == "azure_blob":
        return AzureBlobObjectStore(st.blob_account_url, chunk=settings.ingestion.stream_buffer_bytes * 4,
                                    containers=(st.uploads_container, st.evidence_container, st.context_container))
    return LocalObjectStore(settings.resolve_path(st.object_store_root), chunk=settings.ingestion.stream_buffer_bytes)


def iter_chunks(stream: BinaryIO, size: int) -> Iterator[bytes]:
    while True:
        buf = stream.read(size)
        if not buf:
            return
        yield buf
