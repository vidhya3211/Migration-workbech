"""Azure Blob object store against the Azurite emulator (the same SDK calls as Azure Blob Storage).

Runs only when ``AIWB_BLOB_CONNECTION_STRING`` points at an emulator, e.g.::

    npx azurite-blob --inMemoryPersistence --blobPort 10000
    AIWB_BLOB_CONNECTION_STRING="DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=...;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
"""
import hashlib
import io
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("AIWB_BLOB_CONNECTION_STRING"),
                                reason="needs AIWB_BLOB_CONNECTION_STRING (Azurite or a dev storage account)")


def _store():
    from storage.objects import AzureBlobObjectStore
    return AzureBlobObjectStore("", chunk=64 * 1024, containers=("uploads", "evidence", "context"))


def test_blocks_ranges_and_chunked_commit():
    store = _store()
    key = f"test/{uuid.uuid4().hex}/bundle.bin"
    data = os.urandom(300 * 1024 + 17)                       # several 64 KiB blocks plus a tail
    uri, size, sha = store.put_stream("evidence", key, io.BytesIO(data))
    assert size == len(data) and sha == hashlib.sha256(data).hexdigest()
    assert store.size(uri) == len(data) and store.exists(uri)
    with store.open(uri) as fh:                              # ranged reads, as evidence verification does
        fh.seek(100_000)
        assert fh.read(5000) == data[100_000:105_000]
        fh.seek(0)
        assert fh.read() == data
    parts = [data[i:i + 100_000] for i in range(0, len(data), 100_000)]
    ukey = f"test/{uuid.uuid4().hex}/upload.zip"
    for i in reversed(range(len(parts))):                    # browsers may send chunks out of order
        store.stage_part("uploads", ukey, i, parts[i])
    uri2, size2 = store.commit_parts("uploads", ukey, len(parts))
    assert size2 == len(data) and store.get_bytes(uri2) == data
    store.delete(uri)
    assert not store.exists(uri)


def test_golden_pipeline_on_azure_blob():
    from evals.harness import Workspace
    ws = Workspace.create(**{"storage.object_store": "azure_blob"})
    try:
        assert ws.services.objects.backend == "azure_blob"
        run_id = ws.run_golden_pipeline()
        status = ws.runs.status(run_id)
        assert status["status"] == "completed", status
        assert status["progress"]["steps_completed"] == status["progress"]["steps_total"]
    finally:
        ws.close()
