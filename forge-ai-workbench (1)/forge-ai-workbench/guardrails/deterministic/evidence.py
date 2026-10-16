"""Evidence verification: does the cited excerpt really exist at the cited locator in the raw source?

This is the heart of the certification boundary. LLM output, heuristic output, ATS candidates and MCP
output are all untrusted until their evidence re-verifies against the evidence store byte-for-byte. Data
evidence (customer records) re-verifies against the rows staged in PostgreSQL; ``prefetch`` batches those
look-ups so millions of breaks verify in a handful of queries.
"""
from __future__ import annotations

from collections import defaultdict
from functools import lru_cache

from ingestion.evidence_store import EvidenceReader
from schemas.artefact import Artefact
from schemas.common import value_ref
from schemas.provenance import CodeLocator, DataLocator, DocumentLocator, Evidence, SchemaLocator, SheetLocator


class EvidenceVerifier:
    def __init__(self, reader: EvidenceReader, artefacts: dict[str, Artefact], staging=None) -> None:
        self.reader = reader
        self.artefacts = artefacts
        self.staging = staging
        self._cell = lru_cache(maxsize=256)(self._read_cell)
        self._rows: dict[tuple[str, int], list[str] | None] = {}

    def prefetch(self, evidence: list[Evidence], max_rows: int = 200_000) -> None:
        """Load the staged rows a batch of data evidence cites, one query per artefact."""
        if self.staging is None:
            return
        want: dict[str, set[int]] = defaultdict(set)
        for ev in evidence:
            if isinstance(ev.locator, DataLocator) and (ev.artefact_id, ev.locator.row_number) not in self._rows:
                want[ev.artefact_id].add(ev.locator.row_number)
        if len(self._rows) > max_rows:
            self._rows.clear()
        for aid, lines in want.items():
            got = self.staging.rows_by_lines(aid, sorted(lines))
            for ln in lines:
                self._rows[(aid, ln)] = got.get(ln)

    def _data_row(self, artefact_id: str, line_no: int) -> list[str] | None:
        key = (artefact_id, line_no)
        if key not in self._rows:
            self._rows[key] = self.staging.row(artefact_id, line_no) if self.staging is not None else None
        return self._rows[key]

    def _read_cell(self, uri: str, sheet: str, cell: str) -> str | None:
        from tools.parsers.spreadsheet import cell_repr, load_workbook_bytes
        wb = load_workbook_bytes(self.reader.bytes(uri))
        if sheet not in wb.sheetnames:
            return None
        return cell_repr(wb[sheet][cell.split(":")[0]])

    def _binary_doc_lines(self, uri: str, fmt: str, page: int | None) -> list[str]:
        from tools.parsers.document import lines_for
        return [dl.text for dl in lines_for(fmt, None, self.reader.bytes(uri)) if dl.page == page]

    def verify(self, ev: Evidence, *, run_artefacts_only: bool = True) -> str | None:
        """Return a rejection reason, or None when the evidence is exact."""
        art = self.artefacts.get(ev.artefact_id)
        if art is None:
            return f"artefact {ev.artefact_id} is not part of this run" if run_artefacts_only else None
        if ev.source_sha256 != art.sha256:
            return "evidence cites a different version of the artefact (sha256 mismatch)"
        if ev.system != art.system:
            return f"evidence system {ev.system.system_name}/{ev.system.system_role} does not match the artefact"
        loc = ev.locator
        if getattr(loc, "file", None) != art.path:
            return f"locator file {getattr(loc, 'file', None)!r} does not match artefact path {art.path!r}"
        match loc:
            case DocumentLocator() if art.path.lower().endswith((".pdf", ".docx")):
                lines = self._binary_doc_lines(art.blob_uri, art.path.lower().rsplit(".", 1)[-1], loc.page)
                if not (1 <= loc.line_start <= loc.line_end <= len(lines)):
                    return f"locator lines {loc.line_start}-{loc.line_end} outside page/document"
                if "\n".join(lines[loc.line_start - 1: loc.line_end]) != ev.excerpt:
                    return "excerpt does not match the document at the cited location"
            case CodeLocator() | DocumentLocator() | SchemaLocator():
                lines = self.reader.lines(art.blob_uri)
                if not (1 <= loc.line_start <= loc.line_end <= len(lines)):
                    return f"locator lines {loc.line_start}-{loc.line_end} outside file (1-{len(lines)})"
                actual = "\n".join(lines[loc.line_start - 1: loc.line_end])
                if actual != ev.excerpt:
                    return "excerpt does not match the source at the cited lines"
            case SheetLocator():
                actual = self._cell(art.blob_uri, loc.sheet, loc.cell_range)
                if actual is None:
                    return f"sheet {loc.sheet!r} not found"
                if actual != ev.excerpt:
                    return f"excerpt does not match cell {loc.sheet}!{loc.cell_range}"
            case DataLocator():
                staged = self.staging.table(art.artefact_id) if self.staging is not None else None
                if staged is None:
                    return "data evidence cites an extract that is not staged"
                header = staged.columns
                row = self._data_row(art.artefact_id, loc.row_number)
                if row is None:
                    return f"row {loc.row_number} outside extract"
                if loc.column is None:
                    expected = value_ref(tuple(row))
                else:
                    if loc.column not in header:
                        return f"column {loc.column!r} not in extract"
                    idx = header.index(loc.column)
                    expected = value_ref(row[idx] if idx < len(row) else None)
                if ev.excerpt != expected:
                    return "data evidence reference does not match the extract"
        return None
