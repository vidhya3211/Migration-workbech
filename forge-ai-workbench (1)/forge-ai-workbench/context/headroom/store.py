"""Context store for Headroom's Compress-Cache-Retrieve: originals stay retrievable.

Originals are kept in the object store's ``context`` container (Azure Blob Storage in Azure) under the same
classification and retention as the underlying evidence; never logged. Headroom is not the source of truth -
this store only lets an agent recover what was compressed away.
"""
from __future__ import annotations

import hashlib
import json

from storage.objects import ObjectStore, make_uri


class ContextStore:
    def __init__(self, objects: ObjectStore, container: str) -> None:
        self.objects = objects
        self.container = container

    def put(self, run_id: str, content: str, classification: str) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        _check(run_id, digest)
        key = f"{run_id}/{digest[:32]}.json"
        if not self.objects.exists(make_uri(self.container, key)):
            self.objects.put_bytes(self.container, key, json.dumps({"classification": classification,
                                                                    "content": content}).encode("utf-8"))
        return f"ctx://{run_id}/{digest[:32]}"

    def get(self, ref: str) -> str:
        if not ref.startswith("ctx://"):
            raise ValueError(ref)
        run_id, digest = ref[len("ctx://"):].split("/", 1)
        _check(run_id, digest)
        data = self.objects.get_bytes(make_uri(self.container, f"{run_id}/{digest}.json"))
        return json.loads(data.decode("utf-8"))["content"]


def _check(run_id: str, digest: str) -> None:
    if not all(c.isalnum() or c in "-_" for c in run_id + digest):
        raise ValueError("invalid context reference")
