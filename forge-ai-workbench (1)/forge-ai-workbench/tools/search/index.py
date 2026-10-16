"""Search contracts over certified content. A search hit is a *pointer* to certified truth in PostgreSQL -
never truth itself."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
STOP = {"the", "a", "an", "of", "is", "are", "to", "in", "for", "and", "or", "what", "which", "how", "does", "do",
        "on", "by", "at", "be", "it", "this", "that", "with"}


def terms(text: str) -> list[str]:
    raw = _WORD.findall(re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower().replace("_", " ").replace("-", " "))
    return [t for t in raw if t not in STOP]


@dataclass
class SearchDocument:
    doc_id: str
    kind: str                 # certified_rule | adjudication | mapping
    title: str
    text: str
    ref: str                  # knowledge-core reference (entity:id)
    metadata: dict = field(default_factory=dict)


@dataclass
class SearchHit:
    doc: SearchDocument
    score: float


class SearchBackend(Protocol):
    minimum_score: float
    requires_term_overlap: bool

    def index(self, docs: list[SearchDocument], *, project_id: str, run_id: str) -> None: ...

    def search(self, query: str, *, project_id: str, run_id: str | None = None, top: int = 5,
               kinds: set[str] | None = None) -> list[SearchHit]: ...
