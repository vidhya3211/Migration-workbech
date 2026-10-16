"""Data-extract access for Stage-5 agents: binds each staged extract to its dictionary entity and builds data
evidence - without ever loading an extract into memory.

Rows come from PostgreSQL (``staging.extracts``): streamed through server-side cursors, or summarised with SQL
aggregates. Customer values are read from the controlled store only; provenance carries value *references*.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from schemas.artefact import Artefact
from schemas.common import value_ref
from schemas.mapping import DictionaryEntry
from schemas.provenance import DataLocator, Evidence
from staging.extracts import ExtractStaging, StagedExtract


@dataclass
class Extract:
    artefact: Artefact
    staged: StagedExtract
    staging: ExtractStaging
    entity: str | None
    fields: dict[str, DictionaryEntry] = field(default_factory=dict)   # column -> dictionary entry
    key: str | None = None

    @property
    def header(self) -> list[str]:
        return self.staged.columns

    @property
    def row_count(self) -> int:
        return self.staged.row_count

    def idx(self, column: str) -> int:
        return self.header.index(column)

    def value(self, row: list[str], column: str) -> str | None:
        i = self.idx(column)
        return row[i] if i < len(row) else None

    def record_key(self, row: list[str]) -> str:
        return (self.value(row, self.key) or "").strip() if self.key else value_ref(tuple(row))

    # ---- streaming / SQL access
    def rows(self, order_by: str | None = None) -> Iterator[tuple[int, list[str]]]:
        return self.staging.iter_rows(self.artefact.artefact_id,
                                      order_by=None if order_by is None else self.idx(order_by))

    def find(self, column: str, value: str, limit: int = 100) -> list[tuple[int, list[str]]]:
        return self.staging.rows_where(self.artefact.artefact_id, self.idx(column), value, limit)

    def row(self, line_no: int) -> list[str] | None:
        return self.staging.row(self.artefact.artefact_id, line_no)

    def rows_at(self, lines: list[int]) -> dict[int, list[str]]:
        return self.staging.rows_by_lines(self.artefact.artefact_id, lines)

    def stats(self, column: str, top: int = 5) -> dict:
        return self.staging.column_stats(self.artefact.artefact_id, self.idx(column), top)

    def sample(self, column: str, n: int) -> list[str]:
        return self.staging.sample(self.artefact.artefact_id, self.idx(column), n)

    def distinct(self, column: str, cap: int) -> tuple[list[tuple[str, int]], bool]:
        return self.staging.distinct_values(self.artefact.artefact_id, self.idx(column), cap)

    def duplicates(self, column: str) -> list[tuple[str, list[int]]]:
        return self.staging.duplicates(self.artefact.artefact_id, self.idx(column))

    def blocks(self, column: str, max_block: int) -> Iterator[tuple[str, list[int], bool]]:
        return self.staging.blocks(self.artefact.artefact_id, self.idx(column), max_block)

    # ---- evidence
    def evidence(self, line_no: int, row: list[str], column: str | None = None) -> Evidence:
        """``line_no`` is the record's physical line in the staged extract (header = 1)."""
        excerpt = value_ref(tuple(row)) if column is None else value_ref(self.value(row, column))
        return Evidence(artefact_id=self.artefact.artefact_id, source_sha256=self.artefact.sha256,
                        system=self.artefact.system, excerpt=excerpt,
                        locator=DataLocator(file=self.artefact.path, row_number=line_no,
                                            record_key=self.record_key(row), column=column))


def load_extracts(services, run_id: str, role: str, dictionary: list[DictionaryEntry]) -> list[Extract]:
    out = []
    entries = [d for d in dictionary if d.system.system_role.value == role]
    by_entity: dict[str, dict[str, DictionaryEntry]] = {}
    for e in entries:
        by_entity.setdefault(e.entity, {})[e.field.upper()] = e
    arts = services.artefacts(run_id)
    for staged in services.staging.tables(run_id, role):
        art = arts.get(staged.artefact_id)
        if art is None:
            continue
        header = staged.columns
        best, overlap = None, 0
        for entity, cols in by_entity.items():
            n = sum(1 for h in header if h.upper() in cols)
            if n > overlap:
                best, overlap = entity, n
        fields = {h: by_entity[best][h.upper()] for h in header if best and h.upper() in by_entity[best]}
        key = next((h for h, e in fields.items() if e.primary_key), None) or (header[0] if header else None)
        out.append(Extract(art, staged, services.staging,
                           best if overlap >= max(1, len(header) // 2) else None, fields, key))
    return out
