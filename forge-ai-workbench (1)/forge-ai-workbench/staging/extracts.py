"""Data-extract staging in PostgreSQL.

Data extracts (CSV / pipe / tab delimited, or fixed-width EBCDIC decoded through a copybook) are loaded with
``COPY`` into ``extract_row`` - one row per record, the values a JSON array aligned with the extract's
columns - so a multi-GB extract never has to be held in memory. Agents read through this module:

* streaming iteration over server-side cursors (``iter_rows``),
* SQL aggregates for statistics, distinct values, duplicates and blocking,
* point look-ups by line number (the deterministic guard re-verifies data evidence here) or by value.

``line_no`` is the record's physical position in the staged file (the header is line 1), which is what a
``DataLocator.row_number`` cites.
"""
from __future__ import annotations

import codecs
import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import BinaryIO, Iterable, Iterator

from sqlalchemy import Engine, delete, select, text

from db import models as m

csv.field_size_limit(16 * 1024 * 1024)


@dataclass(frozen=True)
class StagedExtract:
    artefact_id: str
    run_id: str
    system_name: str
    system_role: str
    path: str
    format: str
    delimiter: str | None
    columns: list[str]
    row_count: int
    first_line: int
    controls: dict


class StagingError(ValueError):
    pass


def sniff_delimiter(sample: str, candidates: list[str]) -> str:
    first = sample.split("\n", 1)[0]
    counts = {d: first.count(d) for d in candidates}
    best = max(counts, key=lambda d: counts[d])
    if counts[best] < 1:
        raise StagingError("could not determine the extract's delimiter from its header line")
    return best


def text_stream(stream: BinaryIO, encoding: str) -> io.TextIOBase:
    enc = "utf-8-sig" if encoding in ("utf-8", "utf8") else encoding
    return io.TextIOWrapper(stream, encoding=enc, errors="strict", newline="")


class ExtractStaging:
    def __init__(self, engine: Engine, *, copy_batch_rows: int = 50_000, read_batch_rows: int = 10_000) -> None:
        self.engine = engine
        self.copy_batch_rows = copy_batch_rows
        self.read_batch_rows = read_batch_rows
        self._tables: dict[str, StagedExtract] = {}

    # ================================================================== write
    def stage_rows(self, *, artefact_id: str, project_id: str, run_id: str, system_name: str, system_role: str,
                   path: str, columns: list[str], rows: Iterable[tuple[int, list[str]]], fmt: str,
                   delimiter: str | None = None, first_line: int = 2, controls: dict | None = None) -> StagedExtract:
        """COPY ``(line_no, values)`` rows into ``extract_row`` in batches; idempotent per artefact."""
        n = 0
        with self.engine.begin() as conn:
            conn.execute(delete(m.ExtractRow).where(m.ExtractRow.artefact_id == artefact_id))
            conn.execute(delete(m.ExtractTable).where(m.ExtractTable.artefact_id == artefact_id))
            pg = conn.connection.driver_connection
            with pg.cursor() as cur:
                with cur.copy("COPY extract_row (artefact_id, line_no, vals) FROM STDIN") as cp:
                    for line_no, row in rows:
                        cp.write_row((artefact_id, line_no, json.dumps(row, ensure_ascii=False)))
                        n += 1
            conn.execute(m.ExtractTable.__table__.insert().values(
                artefact_id=artefact_id, project_id=project_id, run_id=run_id, system_name=system_name,
                system_role=system_role, path=path, format=fmt, delimiter=delimiter, columns=columns, row_count=n,
                first_line=first_line, controls=controls or {}, staged_at=datetime.now(timezone.utc)))
        self._tables.pop(artefact_id, None)
        return self.table(artefact_id)

    def stage_delimited(self, *, artefact_id: str, project_id: str, run_id: str, system_name: str, system_role: str,
                        path: str, stream: BinaryIO, encoding: str, delimiters: list[str]) -> StagedExtract:
        txt = text_stream(stream, encoding)
        head = txt.read(65536)
        delimiter = sniff_delimiter(head, delimiters)
        reader = csv.reader(_chain_text(head, txt), delimiter=delimiter)
        try:
            header = [h.strip() for h in next(reader)]
        except StopIteration:
            raise StagingError(f"{path}: empty extract") from None
        if len(set(h.upper() for h in header)) != len(header):
            raise StagingError(f"{path}: duplicate column names in the header")

        def rows() -> Iterator[tuple[int, list[str]]]:
            line = 1
            for rec in reader:
                line += 1
                if not any(c.strip() for c in rec):
                    continue
                yield line, rec
        return self.stage_rows(artefact_id=artefact_id, project_id=project_id, run_id=run_id,
                               system_name=system_name, system_role=system_role, path=path, columns=header,
                               rows=rows(), fmt="delimited", delimiter=delimiter, first_line=2)

    @staticmethod
    def _streaming(conn):
        """Server-side cursors are planned for the first 10% of rows by default (``cursor_tuple_fraction``),
        which turns whole-extract joins into nested loops; these cursors are always read to the end."""
        conn.exec_driver_sql("SET LOCAL cursor_tuple_fraction = 1.0")
        return conn

    def drop_extract(self, artefact_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(delete(m.ExtractRow).where(m.ExtractRow.artefact_id == artefact_id))
            conn.execute(delete(m.ExtractTable).where(m.ExtractTable.artefact_id == artefact_id))
        self._tables.pop(artefact_id, None)

    def drop_run(self, run_id: str) -> None:
        with self.engine.begin() as conn:
            ids = [r[0] for r in conn.execute(select(m.ExtractTable.artefact_id).where(m.ExtractTable.run_id == run_id))]
            if ids:
                conn.execute(delete(m.ExtractRow).where(m.ExtractRow.artefact_id.in_(ids)))
                conn.execute(delete(m.ExtractTable).where(m.ExtractTable.run_id == run_id))
        for i in list(self._tables):
            if self._tables[i].run_id == run_id:
                self._tables.pop(i, None)

    # ================================================================== read
    def table(self, artefact_id: str) -> StagedExtract | None:
        hit = self._tables.get(artefact_id)
        if hit is not None:
            return hit
        with self.engine.connect() as conn:
            r = conn.execute(select(m.ExtractTable).where(m.ExtractTable.artefact_id == artefact_id)).mappings().first()
        if r is None:
            return None
        se = StagedExtract(artefact_id=r["artefact_id"], run_id=r["run_id"], system_name=r["system_name"],
                           system_role=r["system_role"], path=r["path"], format=r["format"], delimiter=r["delimiter"],
                           columns=list(r["columns"]), row_count=int(r["row_count"]), first_line=int(r["first_line"]),
                           controls=dict(r["controls"] or {}))
        self._tables[artefact_id] = se
        return se

    def tables(self, run_id: str, role: str | None = None) -> list[StagedExtract]:
        q = select(m.ExtractTable.artefact_id).where(m.ExtractTable.run_id == run_id).order_by(m.ExtractTable.path)
        if role:
            q = q.where(m.ExtractTable.system_role == role)
        with self.engine.connect() as conn:
            ids = [r[0] for r in conn.execute(q)]
        return [t for t in (self.table(i) for i in ids) if t is not None]

    def iter_rows(self, artefact_id: str, *, order_by: int | None = None,
                  batch: int | None = None) -> Iterator[tuple[int, list[str]]]:
        """Stream ``(line_no, values)``; ``order_by`` sorts by a column index (merge joins) instead of line."""
        order = "line_no" if order_by is None else f"(vals ->> {int(order_by)}), line_no"
        sql = text(f"SELECT line_no, vals FROM extract_row WHERE artefact_id = :a ORDER BY {order}")
        with self.engine.connect() as conn:
            res = self._streaming(conn).execution_options(stream_results=True, max_row_buffer=batch or self.read_batch_rows) \
                .execute(sql, {"a": artefact_id})
            for line_no, vals in res:
                yield int(line_no), list(vals)

    def row(self, artefact_id: str, line_no: int) -> list[str] | None:
        with self.engine.connect() as conn:
            r = conn.execute(text("SELECT vals FROM extract_row WHERE artefact_id = :a AND line_no = :n"),
                             {"a": artefact_id, "n": line_no}).first()
        return list(r[0]) if r else None

    def rows_where(self, artefact_id: str, col: int, value: str, limit: int = 100) -> list[tuple[int, list[str]]]:
        with self.engine.connect() as conn:
            rs = conn.execute(text("SELECT line_no, vals FROM extract_row WHERE artefact_id = :a "
                                   "AND btrim(vals ->> :c) = :v ORDER BY line_no LIMIT :l"),
                              {"a": artefact_id, "c": col, "v": value.strip(), "l": limit})
            return [(int(n), list(v)) for n, v in rs]

    def column_stats(self, artefact_id: str, col: int, top: int = 5) -> dict:
        with self.engine.connect() as conn:
            s = conn.execute(text(
                "SELECT count(*) AS n, "
                "count(*) FILTER (WHERE coalesce(btrim(vals ->> :c), '') = '') AS nulls, "
                "count(DISTINCT nullif(btrim(vals ->> :c), '')) AS distinct_n, "
                "min(nullif(btrim(vals ->> :c), '')) AS min_v, max(nullif(btrim(vals ->> :c), '')) AS max_v "
                "FROM extract_row WHERE artefact_id = :a"), {"a": artefact_id, "c": col}).mappings().first()
            tops = conn.execute(text(
                "SELECT btrim(vals ->> :c) AS v, count(*) AS k FROM extract_row WHERE artefact_id = :a "
                "AND coalesce(btrim(vals ->> :c), '') <> '' GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT :t"),
                {"a": artefact_id, "c": col, "t": top}).all()
        return {"rows": int(s["n"]), "nulls": int(s["nulls"]), "distinct": int(s["distinct_n"]),
                "min": s["min_v"], "max": s["max_v"], "top": [(v, int(k)) for v, k in tops]}

    TYPE_PATTERNS = {"integer": "^-?[0-9]+$", "decimal": "^-?[0-9]+(\\.[0-9]+)?$",
                     "date": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$", "boolean": "^(true|false|y|n|t|f)$"}

    def violations(self, artefact_id: str, col: int, expected_type: str | None) -> int:
        """Non-empty values that do not match the expected type's lexical pattern (counted in SQL)."""
        pattern = self.TYPE_PATTERNS.get(expected_type or "")
        if pattern is None:
            return 0
        with self.engine.connect() as conn:
            return int(conn.execute(text(
                "SELECT count(*) FROM extract_row WHERE artefact_id = :a AND coalesce(btrim(vals ->> :c), '') <> '' "
                "AND NOT (btrim(vals ->> :c) ~* :p)"), {"a": artefact_id, "c": col, "p": pattern}).scalar() or 0)

    def distinct_values(self, artefact_id: str, col: int, cap: int) -> tuple[list[tuple[str, int]], bool]:
        """(value, count) for up to ``cap`` distinct raw values; second item says whether it was truncated."""
        with self.engine.connect() as conn:
            rs = conn.execute(text("SELECT vals ->> :c AS v, count(*) FROM extract_row WHERE artefact_id = :a "
                                   "GROUP BY 1 ORDER BY 2 DESC LIMIT :l"), {"a": artefact_id, "c": col, "l": cap + 1}).all()
        return [(v, int(k)) for v, k in rs[:cap]], len(rs) > cap

    def sample(self, artefact_id: str, col: int, n: int) -> list[str]:
        with self.engine.connect() as conn:
            rs = conn.execute(text("SELECT vals ->> :c FROM extract_row WHERE artefact_id = :a ORDER BY line_no "
                                   "LIMIT :n"), {"a": artefact_id, "c": col, "n": n}).all()
        return [r[0] if r[0] is not None else "" for r in rs]

    def duplicates(self, artefact_id: str, col: int, limit_groups: int = 10_000) -> list[tuple[str, list[int]]]:
        with self.engine.connect() as conn:
            rs = conn.execute(text(
                "SELECT btrim(vals ->> :c) AS k, array_agg(line_no ORDER BY line_no) FROM extract_row "
                "WHERE artefact_id = :a GROUP BY 1 HAVING count(*) > 1 ORDER BY 1 LIMIT :l"),
                {"a": artefact_id, "c": col, "l": limit_groups}).all()
        return [(k or "", [int(x) for x in ids]) for k, ids in rs]

    def blocks(self, artefact_id: str, col: int, max_block: int) -> Iterator[tuple[str, list[int], bool]]:
        """Blocking for record linkage: (key, line_nos, oversized) for every key shared by 2+ records."""
        sql = text("SELECT btrim(vals ->> :c) AS k, count(*) AS n, "
                   "(array_agg(line_no ORDER BY line_no))[1:(:mx + 1)] FROM extract_row WHERE artefact_id = :a "
                   "AND coalesce(btrim(vals ->> :c), '') <> '' GROUP BY 1 HAVING count(*) > 1 ORDER BY 1")
        with self.engine.connect() as conn:
            res = self._streaming(conn).execution_options(stream_results=True).execute(sql, {"a": artefact_id, "c": col, "mx": max_block})
            for k, n, ids in res:
                yield k, [int(x) for x in ids][:max_block], int(n) > max_block

    # a date written YYYY-MM-DD, YYYYMMDD, DD/MM/YYYY, DD-MM-YYYY or DD.MM.YYYY becomes YYYYMMDD, so the same
    # birth date in two files' formats lands in the same block
    _DATE_KEY = ("(CASE WHEN {v} ~ '^[0-9]{{4}}-?[0-9]{{2}}-?[0-9]{{2}}$' THEN replace({v}, '-', '') "
                 "WHEN {v} ~ '^[0-9]{{2}}[/.-][0-9]{{2}}[/.-][0-9]{{4}}$' "
                 "THEN substr({v}, 7, 4) || {mm} || {dd} ELSE {v} END)")

    @classmethod
    def date_key_sql(cls, v: str, order: str = "dmy") -> str:
        """SQL turning a written date into YYYYMMDD; ``order`` says whether 01/02/2026 is day- or month-first."""
        mm, dd = ("substr({v}, 1, 2)", "substr({v}, 4, 2)") if order == "mdy" else ("substr({v}, 4, 2)", "substr({v}, 1, 2)")
        return cls._DATE_KEY.format(v=v, mm=mm.format(v=v), dd=dd.format(v=v))

    def blocks_multi(self, specs: list[tuple[str, list[int]]], max_block: int, date_positions: set[int] | None = None,
                     date_order: str = "dmy") -> Iterator[tuple[str, list[tuple[str, int]], bool]]:
        """Blocking across several extracts: ``specs`` = [(artefact_id, [column indexes])]; the block key is the
        upper-cased, trimmed columns joined with ``|`` (all must be non-empty). Yields
        ``(key, [(artefact_id, line_no)], oversized)`` for keys shared by 2+ records."""
        if not specs:
            return
        parts = []
        params: dict = {"mx": max_block}
        for i, (aid, cols) in enumerate(specs):
            key = " || '|' || ".join(
                (self.date_key_sql(f"btrim(vals ->> :c{i}_{j})", date_order) if j in (date_positions or set())
                 else f"upper(btrim(vals ->> :c{i}_{j}))") for j in range(len(cols)))
            nonempty = " AND ".join(f"coalesce(btrim(vals ->> :c{i}_{j}), '') <> ''" for j in range(len(cols)))
            parts.append(f"SELECT {key} AS k, artefact_id, line_no FROM extract_row WHERE artefact_id = :a{i} AND {nonempty}")
            params[f"a{i}"] = aid
            for j, c in enumerate(cols):
                params[f"c{i}_{j}"] = c
        sql = text("SELECT k, count(*) AS n, (array_agg(artefact_id || ':' || line_no ORDER BY artefact_id, line_no))"
                   f"[1:(:mx + 1)] FROM ({' UNION ALL '.join(parts)}) x GROUP BY k HAVING count(*) > 1 ORDER BY k")
        with self.engine.connect() as conn:
            res = self._streaming(conn).execution_options(stream_results=True).execute(sql, params)
            for k, n, ids in res:
                members = [(x.rsplit(":", 1)[0], int(x.rsplit(":", 1)[1])) for x in ids][:max_block]
                yield k, members, int(n) > max_block

    def rows_by_lines(self, artefact_id: str, lines: list[int]) -> dict[int, list[str]]:
        if not lines:
            return {}
        with self.engine.connect() as conn:
            rs = conn.execute(text("SELECT line_no, vals FROM extract_row WHERE artefact_id = :a "
                                   "AND line_no = ANY(:ls)"), {"a": artefact_id, "ls": list(lines)}).all()
        return {int(n): list(v) for n, v in rs}

    # ================================================================== reconciliation join (SQL side)
    def load_recon_keys(self, run_id: str, pair_id: str, keys: Iterable[tuple[str, int]]) -> int:
        """COPY ``(transformed source key, source line)`` pairs for one source->target entity pair."""
        n = 0
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM recon_key WHERE run_id = :r AND pair_id = :p"), {"r": run_id, "p": pair_id})
            pg = conn.connection.driver_connection
            with pg.cursor() as cur:
                with cur.copy("COPY recon_key (run_id, pair_id, key, line_no) FROM STDIN") as cp:
                    for key, line_no in keys:
                        cp.write_row((run_id, pair_id, key, line_no))
                        n += 1
            conn.execute(text("ANALYZE recon_key"))
        return n

    _TARGET = ("SELECT DISTINCT ON (btrim(vals ->> :tk)) line_no, vals, btrim(vals ->> :tk) AS key "
               "FROM extract_row WHERE artefact_id = :ta ORDER BY btrim(vals ->> :tk), line_no DESC")
    # date-aligned: a record is identified by key AND effective date, so values only compare at the same date
    _TARGET_DATED = ("SELECT DISTINCT ON (k) line_no, vals, k AS key FROM (SELECT line_no, vals, "
                     "btrim(vals ->> :tk) || '|' || coalesce(btrim(vals ->> :td), '') AS k FROM extract_row "
                     "WHERE artefact_id = :ta) x ORDER BY k, line_no DESC")

    def _target_sql(self, target_date_col: int | None) -> str:
        return self._TARGET if target_date_col is None else self._TARGET_DATED

    def recon_source_join(self, run_id: str, pair_id: str, source_artefact: str, target_artefact: str,
                          target_key_col: int, target_date_col: int | None = None
                          ) -> Iterator[tuple[str, int, list[str], int | None, list[str] | None]]:
        """Stream every source record with its matching target record (last occurrence wins for duplicate
        target keys, as a key index would): ``(key, s_line, s_vals, t_line | None, t_vals | None)``.
        With ``target_date_col`` the target key is ``key|effective date`` (the source keys must match)."""
        sql = text(f"WITH t AS ({self._target_sql(target_date_col)}) "
                   "SELECT k.key, k.line_no, s.vals, t.line_no, t.vals FROM recon_key k "
                   "JOIN extract_row s ON s.artefact_id = :sa AND s.line_no = k.line_no "
                   "LEFT JOIN t ON t.key = k.key WHERE k.run_id = :r AND k.pair_id = :p ORDER BY k.line_no")
        params = {"r": run_id, "p": pair_id, "sa": source_artefact, "ta": target_artefact, "tk": target_key_col,
                  "td": target_date_col}
        with self.engine.connect() as conn:
            res = self._streaming(conn).execution_options(stream_results=True, max_row_buffer=self.read_batch_rows).execute(sql, params)
            for key, s_line, s_vals, t_line, t_vals in res:
                yield key, int(s_line), list(s_vals), (int(t_line) if t_line is not None else None), \
                    (list(t_vals) if t_vals is not None else None)

    def recon_unexpected(self, run_id: str, pair_id: str, target_artefact: str,
                         target_key_col: int, target_date_col: int | None = None) -> Iterator[tuple[int, list[str]]]:
        """Target records no source record matched (including superseded duplicates)."""
        sql = text(f"WITH t AS ({self._target_sql(target_date_col)}), matched AS ("
                   "SELECT t.line_no FROM t JOIN (SELECT DISTINCT key FROM recon_key WHERE run_id = :r "
                   "AND pair_id = :p) k ON k.key = t.key) "
                   "SELECT x.line_no, x.vals FROM extract_row x WHERE x.artefact_id = :ta "
                   "AND NOT EXISTS (SELECT 1 FROM matched m WHERE m.line_no = x.line_no) ORDER BY x.line_no")
        params = {"r": run_id, "p": pair_id, "ta": target_artefact, "tk": target_key_col, "td": target_date_col}
        with self.engine.connect() as conn:
            res = self._streaming(conn).execution_options(stream_results=True, max_row_buffer=self.read_batch_rows).execute(sql, params)
            for line_no, vals in res:
                yield int(line_no), list(vals)

    def drop_recon_keys(self, run_id: str, pair_id: str | None = None) -> None:
        with self.engine.begin() as conn:
            if pair_id is None:
                conn.execute(text("DELETE FROM recon_key WHERE run_id = :r"), {"r": run_id})
            else:
                conn.execute(text("DELETE FROM recon_key WHERE run_id = :r AND pair_id = :p"),
                             {"r": run_id, "p": pair_id})

    # ------------------------------------------------------------------ pattern detection (data quality)
    def null_conventions(self, artefact_id: str, col: int, sentinels: list[str]) -> dict[str, int]:
        """How 'no value' is written in a column: blank, a sentinel (configured list, or all 0s / all 9s), or a
        real value."""
        sql = text("SELECT count(*) FILTER (WHERE coalesce(btrim(vals ->> :c), '') = '') AS blank, "
                   "count(*) FILTER (WHERE btrim(vals ->> :c) = ANY(:s) OR btrim(vals ->> :c) ~ '^(0{6,8}|9{6,8}|0000-00-00|9999-12-31|9999-99-99|00/00/0000|99/99/9999)$') "
                   "AS sentinel, count(*) AS n FROM extract_row WHERE artefact_id = :a")
        with self.engine.connect() as conn:
            r = conn.execute(sql, {"a": artefact_id, "c": col, "s": list(sentinels)}).mappings().one()
        blank, sentinel, n = int(r["blank"]), int(r["sentinel"]), int(r["n"])
        return {"blank": blank, "sentinel": sentinel, "real": n - blank - sentinel, "rows": n}

    def lines_where_null_like(self, artefact_id: str, col: int, sentinels: list[str], limit: int) -> list[int]:
        sql = text("SELECT line_no FROM extract_row WHERE artefact_id = :a AND (coalesce(btrim(vals ->> :c), '') = '' "
                   "OR btrim(vals ->> :c) = ANY(:s) OR btrim(vals ->> :c) ~ '^(0{6,8}|9{6,8}|0000-00-00|9999-12-31|9999-99-99|00/00/0000|99/99/9999)$') "
                   "ORDER BY line_no LIMIT :l")
        with self.engine.connect() as conn:
            return [int(x) for x in conn.execute(sql, {"a": artefact_id, "c": col, "s": list(sentinels),
                                                       "l": limit}).scalars()]

    def value_key_span(self, artefact_id: str, col: int, value: str, key_col: int) -> dict:
        """For records holding ``value``: how many, and whether their numeric keys are contiguous (a block written
        by one process) - ``span`` is max - min of the numeric keys."""
        sql = text("SELECT count(*) AS n, min(k) AS lo, max(k) AS hi, count(k) AS nk FROM (SELECT CASE WHEN "
                   "btrim(vals ->> :k) ~ '^[0-9]{1,18}$' THEN (btrim(vals ->> :k))::bigint END AS k FROM extract_row "
                   "WHERE artefact_id = :a AND btrim(vals ->> :c) = :v) x")
        with self.engine.connect() as conn:
            r = conn.execute(sql, {"a": artefact_id, "c": col, "v": value, "k": key_col}).mappings().one()
        return {"n": int(r["n"]), "numeric_keys": int(r["nk"]),
                "span": None if r["lo"] is None else int(r["hi"]) - int(r["lo"])}

    def lines_with_value(self, artefact_id: str, col: int, value: str, limit: int) -> list[int]:
        sql = text("SELECT line_no FROM extract_row WHERE artefact_id = :a AND btrim(vals ->> :c) = :v "
                   "ORDER BY line_no LIMIT :l")
        with self.engine.connect() as conn:
            return [int(x) for x in conn.execute(sql, {"a": artefact_id, "c": col, "v": value, "l": limit}).scalars()]

    def null_rate_by_year(self, artefact_id: str, col: int, date_col: int) -> list[tuple[int, int, int]]:
        """``(year, rows, empty)`` using the leading 4-digit year of ``date_col`` (ISO or YYYYMMDD values)."""
        sql = text("SELECT substr(btrim(vals ->> :d), 1, 4)::int AS y, count(*) AS n, "
                   "count(*) FILTER (WHERE coalesce(btrim(vals ->> :c), '') = '') AS e FROM extract_row "
                   "WHERE artefact_id = :a AND btrim(vals ->> :d) ~ '^(1[89]|20)[0-9]{2}' GROUP BY 1 ORDER BY 1")
        with self.engine.connect() as conn:
            return [(int(y), int(n), int(e)) for y, n, e in conn.execute(sql, {"a": artefact_id, "c": col, "d": date_col})]

    def lines_null_before_year(self, artefact_id: str, col: int, date_col: int, year: int, limit: int) -> tuple[int, list[int]]:
        where = ("artefact_id = :a AND coalesce(btrim(vals ->> :c), '') = '' AND btrim(vals ->> :d) ~ '^(1[89]|20)[0-9]{2}' "
                 "AND substr(btrim(vals ->> :d), 1, 4)::int < :y")
        params = {"a": artefact_id, "c": col, "d": date_col, "y": year, "l": limit}
        with self.engine.connect() as conn:
            n = conn.execute(text(f"SELECT count(*) FROM extract_row WHERE {where}"), params).scalar()
            lines = conn.execute(text(f"SELECT line_no FROM extract_row WHERE {where} ORDER BY line_no LIMIT :l"),
                                 params).scalars()
            return int(n or 0), [int(x) for x in lines]

    def orphans(self, child_artefact: str, child_col: int, parent_artefact: str, parent_col: int,
                limit: int) -> tuple[int, list[int]]:
        """Child records whose reference matches no parent record (anti-join in PostgreSQL)."""
        where = ("c.artefact_id = :ca AND coalesce(btrim(c.vals ->> :cc), '') <> '' AND NOT EXISTS (SELECT 1 FROM "
                 "extract_row p WHERE p.artefact_id = :pa AND btrim(p.vals ->> :pc) = btrim(c.vals ->> :cc))")
        params = {"ca": child_artefact, "cc": child_col, "pa": parent_artefact, "pc": parent_col, "l": limit}
        with self.engine.connect() as conn:
            n = conn.execute(text(f"SELECT count(*) FROM extract_row c WHERE {where}"), params).scalar()
            lines = conn.execute(text(f"SELECT c.line_no FROM extract_row c WHERE {where} ORDER BY c.line_no LIMIT :l"),
                                 params).scalars()
            return int(n or 0), [int(x) for x in lines]

    def distinct_tuples(self, artefact_id: str, cols: list[int], limit: int) -> tuple[list[tuple[list[str], int]], bool]:
        """Distinct value combinations of ``cols`` with their record counts, most frequent first; ``truncated`` when
        there are more than ``limit`` combinations."""
        exprs = ", ".join(f"btrim(vals ->> :c{i})" for i in range(len(cols)))
        params = {"a": artefact_id, "l": limit + 1, **{f"c{i}": c for i, c in enumerate(cols)}}
        sql = text(f"SELECT {exprs}, count(*) AS n FROM extract_row WHERE artefact_id = :a "
                   f"GROUP BY {', '.join(str(i + 1) for i in range(len(cols)))} ORDER BY n DESC LIMIT :l")
        with self.engine.connect() as conn:
            rows = [(list(r[:-1]), int(r[-1])) for r in conn.execute(sql, params)]
        return rows[:limit], len(rows) > limit

    # ------------------------------------------------------------------ control reconciliation
    _NUMERIC = "'^-?[0-9]+(\.[0-9]+)?$'"

    def derived_values(self, target: tuple[str, int, int, int], holdings: tuple[str, int, int, int],
                       prices: tuple[str, int, int, int], date_order: str = "dmy",
                       holdings_date: int | None = None) -> Iterator[tuple[int, str, str, str, object, int, int]]:
        """For every target record ``(artefact, key col, date col, value col)``: the value derived from the source
        holdings ``(artefact, key col, instrument col, quantity col)`` priced by ``(artefact, instrument col,
        date col, price col)`` at the latest price on or before the target's date. With ``holdings_date`` the
        holdings are dated too: each instrument's latest holding on or before the target's date is used (units
        bought or sold between two strikes are not priced at the wrong date). Dates compare in YYYYMMDD form
        whatever their written format. Yields ``(line, key, date, loaded, derived, holdings, priced)``."""
        d = lambda col, alias="": self.date_key_sql(f"btrim({alias}vals ->> {col})", date_order)   # noqa: E731
        num = self._NUMERIC
        if holdings_date is None:
            h_cte = (f"h AS (SELECT btrim(vals ->> :hk) AS k, btrim(vals ->> :hi) AS inst, "
                     f"(btrim(vals ->> :hq))::numeric AS q FROM extract_row WHERE artefact_id = :ha "
                     f"AND btrim(vals ->> :hq) ~ {num}), ")
            h_join = "LEFT JOIN h ON h.k = t.k"
        else:
            h_cte = (f"h0 AS (SELECT btrim(vals ->> :hk) AS k, btrim(vals ->> :hi) AS inst, {d(':hd')} AS dk, "
                     f"(btrim(vals ->> :hq))::numeric AS q FROM extract_row WHERE artefact_id = :ha "
                     f"AND btrim(vals ->> :hq) ~ {num}), ")
            h_join = ("LEFT JOIN LATERAL (SELECT DISTINCT ON (h0.inst) h0.inst, h0.q FROM h0 WHERE h0.k = t.k "
                      "AND h0.dk <= t.dk ORDER BY h0.inst, h0.dk DESC) h ON true")
        sql = text(
            f"WITH t AS (SELECT line_no, btrim(vals ->> :tk) AS k, btrim(vals ->> :td) AS d, {d(':td')} AS dk, "
            "btrim(vals ->> :tv) AS v FROM extract_row WHERE artefact_id = :ta), " + h_cte +
            f"p AS (SELECT btrim(vals ->> :pi) AS inst, {d(':pd')} AS dk, (btrim(vals ->> :pp))::numeric AS pr "
            f"FROM extract_row WHERE artefact_id = :pa AND btrim(vals ->> :pp) ~ {num}) "
            "SELECT t.line_no, t.k, t.d, t.v, sum(h.q * lp.pr) AS derived, count(h.inst) AS n_hold, "
            f"count(lp.pr) AS n_priced FROM t {h_join} LEFT JOIN LATERAL ("
            "SELECT p.pr FROM p WHERE p.inst = h.inst AND p.dk <= t.dk ORDER BY p.dk DESC LIMIT 1) lp ON true "
            "GROUP BY t.line_no, t.k, t.d, t.v ORDER BY t.line_no")
        params = {"ta": target[0], "tk": target[1], "td": target[2], "tv": target[3],
                  "ha": holdings[0], "hk": holdings[1], "hi": holdings[2], "hq": holdings[3],
                  "pa": prices[0], "pi": prices[1], "pd": prices[2], "pp": prices[3], "hd": holdings_date}
        with self.engine.connect() as conn:
            res = self._streaming(conn).execution_options(stream_results=True, max_row_buffer=self.read_batch_rows) \
                .execute(sql, params)
            for line, k, dt, v, derived, n_hold, n_priced in res:
                yield int(line), k or "", dt or "", v or "", derived, int(n_hold), int(n_priced)

    def paired_records(self, left: tuple[str, int], right: tuple[str, int]
                       ) -> Iterator[tuple[str, int, list[str], int | None, list[str] | None]]:
        """Records of two extracts paired on a key (last occurrence wins on each side); right may be absent."""
        sql = text("WITH l AS (SELECT DISTINCT ON (btrim(vals ->> :lk)) line_no, vals, btrim(vals ->> :lk) AS k FROM "
                   "extract_row WHERE artefact_id = :la ORDER BY btrim(vals ->> :lk), line_no DESC), "
                   "r AS (SELECT DISTINCT ON (btrim(vals ->> :rk)) line_no, vals, btrim(vals ->> :rk) AS k FROM "
                   "extract_row WHERE artefact_id = :ra ORDER BY btrim(vals ->> :rk), line_no DESC) "
                   "SELECT l.k, l.line_no, l.vals, r.line_no, r.vals FROM l LEFT JOIN r ON r.k = l.k ORDER BY l.line_no")
        params = {"la": left[0], "lk": left[1], "ra": right[0], "rk": right[1]}
        with self.engine.connect() as conn:
            res = self._streaming(conn).execution_options(stream_results=True, max_row_buffer=self.read_batch_rows) \
                .execute(sql, params)
            for k, ll, lv, rl, rv in res:
                yield k or "", int(ll), list(lv), (int(rl) if rl is not None else None), (list(rv) if rv is not None else None)

    def max_date_like(self, artefact_id: str, col: int) -> str | None:
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT max(btrim(vals ->> :c)) FROM extract_row WHERE artefact_id = :a "
                                     "AND btrim(vals ->> :c) ~ '^[0-9]{4}-?[0-9]{2}-?[0-9]{2}$'"),
                                {"a": artefact_id, "c": col}).scalar()


def _chain_text(head: str, rest: io.TextIOBase) -> Iterator[str]:
    """Re-join the sniffed head with the rest of the stream as an iterable of lines for ``csv.reader``."""
    buf = io.StringIO(head)
    tail = ""
    for line in buf:
        if line.endswith("\n"):
            yield tail + line
            tail = ""
        else:
            tail += line
    for line in rest:
        if tail:
            line, tail = tail + line, ""
        yield line
    if tail:
        yield tail


def decode_name(encoding: str | None) -> str:
    try:
        return codecs.lookup(encoding or "utf-8").name
    except LookupError:
        return "utf-8"
