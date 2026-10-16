"""Mainframe extracts: EBCDIC + COMP-3 + overpunch decoded through the copybook, trailer control totals enforced,
PII tokenised before staging, rows staged in PostgreSQL."""
import io
import json
import zipfile
from decimal import Decimal

import pytest

from db import models as m
from ingestion.mainframe import ControlTotalMismatch, decode_stream

COPYBOOK = """      * Synthetic policy master (multi-record: header / detail / trailer)
       01  HDR-RECORD-00.
           05  HDR-REC-TYPE        PIC X(2).
           05  HDR-FILE-ID         PIC X(8).
       01  POL-RECORD-10.
           05  POL-REC-TYPE        PIC X(2).
           05  POL-NO              PIC 9(6).
           05  POL-STATUS          PIC X(1).
               88  POL-LIVE        VALUE 'A'.
           05  START-DATE          PIC 9(7).
           05  SUM-ASSURED         PIC S9(7)V99 COMP-3.
           05  ANNUAL-PREM         PIC S9(5)V99.
           05  NI-NUMBER           PIC X(9).
           05  FILLER              PIC X(3).
       01  TRL-RECORD-99.
           05  TRL-REC-TYPE        PIC X(2).
           05  TRL-REC-COUNT       PIC 9(6).
           05  TRL-HASH-SA         PIC S9(11)V99 COMP-3.
           05  FILLER              PIC X(18).
"""
LRECL = 2 + 6 + 1 + 7 + 5 + 7 + 9 + 3


def _packed(value: Decimal, digits: int, decimals: int) -> bytes:
    n = int((abs(value) * (10 ** decimals)).to_integral_value())
    s = f"{n:0{digits + decimals}d}" + ("D" if value < 0 else "C")
    if len(s) % 2:
        s = "0" + s
    return bytes.fromhex(s)


def _overpunch(value: Decimal, digits: int, decimals: int) -> bytes:
    s = f"{int((abs(value) * 10 ** decimals).to_integral_value()):0{digits + decimals}d}"
    last = (0xD0 if value < 0 else 0xC0) + int(s[-1])
    return s[:-1].encode("cp037") + bytes([last])


def _file(rows, *, trailer_count=None, trailer_sa=None) -> bytes:
    out = io.BytesIO()
    out.write(("00" + "POLMAST ").encode("cp037").ljust(LRECL, b"\x40"))
    total = Decimal(0)
    for pol, status, start, sa, prem, ni in rows:
        rec = ("10" + f"{pol:06d}" + status + start).encode("cp037") + _packed(sa, 7, 2) + _overpunch(prem, 5, 2) \
            + ni.encode("cp037") + "   ".encode("cp037")
        assert len(rec) == LRECL
        out.write(rec)
        total += sa
    trl = ("99" + f"{trailer_count if trailer_count is not None else len(rows):06d}").encode("cp037") \
        + _packed(trailer_sa if trailer_sa is not None else total, 11, 2)
    out.write(trl.ljust(LRECL, b"\x40"))
    return out.getvalue()


ROWS = [(1001, "A", "1240115", Decimal("150000.00"), Decimal("1200.50"), "QQ123456A"),
        (1002, "L", "0991231", Decimal("75000.25"), Decimal("-15.00"), "QQ654321B")]
DESCRIPTOR = {"lrecl": LRECL, "type_len": 2, "detail_type": "10", "trailer_type": "99",
              "date_formats": {"START-DATE": "cyymmdd"}, "overpunch_fields": ["ANNUAL-PREM"],
              "hash_fields": {"TRL-HASH-SA": "SUM-ASSURED"}}


def test_decoder_reads_packed_overpunch_dates_and_reconciles_the_trailer():
    cols, rows, res = decode_stream(io.BytesIO(_file(ROWS)), COPYBOOK, DESCRIPTOR)
    got = list(rows)
    assert cols == ["POL-NO", "POL-STATUS", "START-DATE", "SUM-ASSURED", "ANNUAL-PREM", "NI-NUMBER"]
    assert got[0][1] == ["1001", "A", "2024-01-15", "150000.00", "1200.50", "QQ123456A"]
    assert got[1][1][2:5] == ["1999-12-31", "75000.25", "-15.00"]
    assert res.controls["reconciled"] is True and res.controls["actual_records"] == 2


def test_control_total_mismatch_halts():
    cols, rows, _ = decode_stream(io.BytesIO(_file(ROWS, trailer_sa=Decimal("1.00"))), COPYBOOK, DESCRIPTOR)
    with pytest.raises(ControlTotalMismatch):
        list(rows)


def _bundle(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for k, v in files.items():
            zf.writestr(k, v)
    return buf.getvalue()


def test_mainframe_extract_is_decoded_tokenised_and_staged(ws):
    pid = ws.project("Mainframe estate", source_technologies=["COBOL"])
    data = _bundle({"data/POLMAST.dat": _file(ROWS), "data/POLMAST.cpy": COPYBOOK.encode(),
                    "data/POLMAST.descriptor.json": json.dumps(DESCRIPTOR).encode()})
    up = ws.runs.upload_bytes(pid, data, "mf.zip", "Legacy MF", "source", "op")
    assert up.status == "ready"
    run_id = ws.runs.start_run(pid, [up.upload_id], "op", background=False)
    arts = {a.path: a for a in ws.services.kc.rows(m.Artefact, run_id)}
    dat = arts["data/POLMAST.dat"]
    assert dat.artefact_type == "data_extract" and dat.ingest["decoder"] == "copybook"
    assert dat.ingest["controls"]["reconciled"] is True and dat.ingest["pii_obfuscated"] == ["NI-NUMBER"]
    staged = ws.services.staging.table(dat.artefact_id)
    assert staged.row_count == 2 and staged.columns[0] == "POL-NO"
    rows = list(ws.services.staging.iter_rows(dat.artefact_id))
    assert rows[0][1][3] == "150000.00" and rows[0][1][5].startswith("tok_")   # raw NI number never staged


def test_mismatched_trailer_halts_the_run(ws):
    pid = ws.project("Bad trailer")
    data = _bundle({"POLMAST.dat": _file(ROWS, trailer_count=3), "POLMAST.cpy": COPYBOOK.encode(),
                    "POLMAST.descriptor.json": json.dumps(DESCRIPTOR).encode()})
    up = ws.runs.upload_bytes(pid, data, "mf.zip", "Legacy MF", "source", "op")
    run_id = ws.runs.start_run(pid, [up.upload_id], "op", background=False)
    st = ws.runs.status(run_id)
    assert st["status"] == "failed" and "control totals" in st["halted_reason"]


ADDR_COPYBOOK = """       01  CUST-RECORD.
           05  CUST-NO             PIC 9(4).
           05  ADDR-TYPE           PIC X(1).
           05  ADDR-UK.
               10  UK-POSTCODE     PIC X(8).
               10  UK-HOUSE        PIC 9(4).
           05  ADDR-OVERSEAS REDEFINES ADDR-UK.
               10  OS-COUNTRY      PIC X(2).
               10  OS-ZONE         PIC S9(9) COMP.
               10  FILLER          PIC X(6).
           05  BALANCE             PIC S9(7)V99 COMP.
           05  VISITS              PIC 9(4) BINARY.
"""


def _cust(no: int, kind: str, body: bytes, balance: Decimal, visits: int) -> bytes:
    return (f"{no:04d}".encode("cp037") + kind.encode("cp037") + body
            + int(balance * 100).to_bytes(4, "big", signed=True) + visits.to_bytes(2, "big", signed=False))


def test_binary_comp_fields_and_redefines_chosen_per_record():
    uk = "LS1 4AP ".encode("cp037") + "0012".encode("cp037")
    overseas = "FR".encode("cp037") + (-123456).to_bytes(4, "big", signed=True) + b"\x40" * 6
    data = _cust(1, "U", uk, Decimal("1234.56"), 3) + _cust(2, "O", overseas, Decimal("-0.75"), 70)
    desc = {"lrecl": 4 + 1 + 12 + 4 + 2,
            "redefines": {"ADDR-OVERSEAS": {"field": "ADDR-TYPE", "equals": "O"}}}
    cols, rows, res = decode_stream(io.BytesIO(data), ADDR_COPYBOOK, desc)
    recs = [dict(zip(cols, r)) for _, r in rows]
    assert {"UK-POSTCODE", "OS-COUNTRY", "OS-ZONE", "BALANCE", "VISITS"} <= set(cols)
    assert recs[0]["UK-POSTCODE"] == "LS1 4AP" and recs[0]["UK-HOUSE"] == "12" and recs[0]["OS-COUNTRY"] == ""
    assert recs[0]["BALANCE"] == "1234.56" and recs[0]["VISITS"] == "3"
    assert recs[1]["OS-COUNTRY"] == "FR" and recs[1]["OS-ZONE"] == "-123456" and recs[1]["UK-POSTCODE"] == ""
    assert recs[1]["BALANCE"] == "-0.75" and recs[1]["VISITS"] == "70"
    # without a rule the original layout decodes strictly; the alternative only where its bytes are valid
    cols, rows, _ = decode_stream(io.BytesIO(data), ADDR_COPYBOOK, {"lrecl": 23})
    recs = [dict(zip(cols, r)) for _, r in rows]
    assert recs[0]["UK-POSTCODE"] == "LS1 4AP" and recs[0]["UK-HOUSE"] == "12"
