"""Fixed-width text ledgers: records decoded by column position from a layout descriptor.

A ``.dat`` / ``.txt`` / ``.fw`` extract is decoded when the same upload carries ``<stem>.descriptor.json`` with
``"format": "fixed_width"`` (no copybook - that path is ``ingestion/mainframe.py``)::

    {"format": "fixed_width", "encoding": "cp1252", "skip_lines": 1,
     "fields": [{"name": "ACCOUNT", "start": 1, "length": 10},
                {"name": "BALANCE", "start": 11, "length": 12, "type": "number", "decimals": 2},
                {"name": "OPENED", "start": 23, "length": 8, "type": "date", "date_format": "%d%m%Y"}],
     "trailer": {"starts_with": "TRL", "count": {"start": 4, "length": 9},
                 "sums": [{"field": "BALANCE", "start": 13, "length": 15, "decimals": 2}]}}

``start`` is 1-based. ``number`` fields accept a leading/trailing sign and an implied decimal point
(``decimals`` places when the text has no ``.``). Records are read one at a time; a record that does not decode
is quarantined with its record number and the reason (never silently dropped), and the trailer's record count
and sums are recomputed from the data - a mismatch with nothing quarantined halts ingestion (data was lost); a
mismatch explained by quarantined records is reported with them.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import BinaryIO, Iterator

from ingestion.mainframe import ControlTotalMismatch, DecodeError

QUARANTINE_KEEP = 200


@dataclass
class FixedWidthResult:
    columns: list[str]
    records: int = 0
    rejected: int = 0
    quarantine: list[dict] = field(default_factory=list)
    controls: dict = field(default_factory=dict)


def is_fixed_width(descriptor: dict | None) -> bool:
    return bool(descriptor) and str(descriptor.get("format", "")).lower() == "fixed_width" and bool(descriptor.get("fields"))


def _slice(line: str, start: int, length: int) -> str:
    return line[start - 1:start - 1 + length]


def _number(text: str, decimals: int) -> Decimal:
    s = text.strip().replace(",", "")
    if not s:
        raise ValueError("empty number")
    sign = 1
    if s.endswith("-") or s.startswith("-"):
        sign, s = -1, s.strip("-")
    elif s.endswith("+") or s.startswith("+"):
        s = s.strip("+")
    v = Decimal(s)
    if "." not in s and decimals:
        v = v.scaleb(-decimals)
    return v * sign


def decode_fixed_width(stream: BinaryIO, descriptor: dict) -> tuple[list[str], Iterator[tuple[int, list[str]]], FixedWidthResult]:
    fields = descriptor["fields"]
    for f in fields:
        if int(f.get("start", 0)) < 1 or int(f.get("length", 0)) < 1:
            raise DecodeError(f"field {f.get('name')}: start and length must be positive")
    columns = [str(f["name"]) for f in fields]
    result = FixedWidthResult(columns=columns)
    trailer = descriptor.get("trailer") or {}
    skip = int(descriptor.get("skip_lines", 0))
    text = io.TextIOWrapper(stream, encoding=descriptor.get("encoding", "utf-8"), newline="")

    def quarantine(n: int, reason: str) -> None:
        result.rejected += 1
        if len(result.quarantine) < QUARANTINE_KEEP:
            result.quarantine.append({"record": n, "reason": reason})

    def gen() -> Iterator[tuple[int, list[str]]]:
        sums = {s["field"]: Decimal(0) for s in trailer.get("sums", [])}
        trl = None
        n = 0
        try:
            for raw in text:
                n += 1
                line = raw.rstrip("\r\n")
                if n <= skip or not line.strip():
                    continue
                if trailer.get("starts_with") and line.startswith(trailer["starts_with"]):
                    trl = line
                    continue
                values, ok = [], True
                for f in fields:
                    v = _slice(line, int(f["start"]), int(f["length"]))
                    kind = f.get("type", "text")
                    try:
                        if kind == "number":
                            v = "" if not v.strip() else format(_number(v, int(f.get("decimals", 0))), "f")
                        elif kind == "date":
                            v = "" if not v.strip() or set(v.strip()) <= {"0", "9"} else \
                                datetime.strptime(v.strip(), f.get("date_format", "%Y%m%d")).date().isoformat()
                        else:
                            v = v.strip()
                    except (ValueError, InvalidOperation) as exc:
                        quarantine(n, f"{f['name']}: {exc}")
                        ok = False
                        break
                    values.append(v)
                if not ok:
                    continue
                result.records += 1
                for s in sums:
                    i = columns.index(s)
                    sums[s] += Decimal(values[i]) if values[i] else Decimal(0)
                yield n, values
        finally:
            text.detach()
        if trailer and trl is not None:
            ok = True
            expected_n = None
            if trailer.get("count"):
                c = trailer["count"]
                expected_n = int(_slice(trl, int(c["start"]), int(c["length"])).strip() or 0)
                ok = expected_n == result.records + result.rejected      # the trailer counts every record
            totals = {}
            for s in trailer.get("sums", []):
                exp = _number(_slice(trl, int(s["start"]), int(s["length"])), int(s.get("decimals", 0)))
                act = sums[s["field"]]
                totals[s["field"]] = {"expected": format(exp, "f"), "actual": format(act, "f")}
                ok = ok and exp.quantize(Decimal("0.01")) == act.quantize(Decimal("0.01"))
            result.controls = {"expected_records": expected_n, "actual_records": result.records,
                               "rejected_records": result.rejected, "totals": totals, "reconciled": ok}
            if not ok and not result.rejected:
                raise ControlTotalMismatch(f"trailer control totals do not reconcile: {result.controls}")
            if not ok:     # explained by quarantined records: staged, and the gap is visible, not hidden
                result.controls["note"] = (f"totals differ by the {result.rejected} quarantined record(s); "
                                           "fix them at source and re-load")
        else:
            result.controls = {"actual_records": result.records, "rejected_records": result.rejected,
                               "reconciled": None, "note": "no trailer record - control totals not verifiable"}
    return columns, gen(), result
