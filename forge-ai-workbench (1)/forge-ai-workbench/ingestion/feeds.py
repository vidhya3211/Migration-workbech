"""Feed interface for future automated ingestion (§32).

The current phase supports console upload only. SFTP / ADF / Service Bus feeds implement ``FeedSource``
later and hand the same ``(bytes, filename, system_name, system_role)`` tuple to ``receive_upload`` -
nothing downstream changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Protocol


@dataclass(frozen=True)
class FeedItem:
    data: bytes
    filename: str
    system_name: str
    system_role: str
    received_by: str


class FeedSource(Protocol):
    name: str

    def poll(self) -> Iterator[FeedItem]: ...


class ConsoleUploadFeed:
    """The only feed in this phase: items pushed by the console upload endpoint."""

    name = "console"

    def __init__(self) -> None:
        self._pending: list[FeedItem] = []

    def push(self, item: FeedItem) -> None:
        self._pending.append(item)

    def poll(self) -> Iterator[FeedItem]:
        while self._pending:
            yield self._pending.pop(0)


class SftpFeed:  # pragma: no cover - deliberately not implemented in this phase
    name = "sftp"

    def poll(self) -> Iterator[FeedItem]:
        raise NotImplementedError("SFTP ingestion is out of scope for the current phase (§32)")
