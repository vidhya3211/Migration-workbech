"""Mainframe data extracts: fixed-length EBCDIC records decoded through their COBOL copybook.

A ``.dat`` extract is decoded when the same upload carries its copybook (``<stem>.cpy``; dots/dashes in the
stem are interchangeable) and, optionally, a descriptor (``<stem>.descriptor.json``) with the record length,
record-type width, date formats, sign-overpunched fields and trailer control totals:

    {"lrecl": 182, "type_len": 2, "detail_type": "10", "trailer_type": "99",
     "date_formats": {"COMMENCE-DATE": "cyymmdd"}, "overpunch_fields": ["ANNUAL-PREM"],
     "hash_fields": {"TRL-HASH-PREM": "ANNUAL-PREM"}}

Without a descriptor a single-layout copybook is used as-is. Supported: PIC X(n) / 9(n) / S9(n)V9(m), COMP-3
(packed decimal), zoned decimal with trailing sign overpunch, OCCURS (elementary and group), 88-levels,
FILLER, binary COMP / COMP-4 / COMP-5 / BINARY (big-endian two's complement, 2 / 4 / 8 bytes), REDEFINES
(the alternative layout shares the original's bytes; a descriptor rule chooses per record, e.g.
``"redefines": {"ADDR-OVERSEAS": {"field": "ADDR-TYPE", "equals": "O"}}`` - without a rule the original decodes
strictly and the alternative only where its bytes are valid), date conventions CYYMMDD / YYMMDD (windowed at 50)
/ YYDDD (Julian), all-zero = no date, all-nine = open-ended. Records are decoded one at a time from a stream; the trailer's record count and hash totals are
recomputed from the bytes and a mismatch halts ingestion (nothing partial is ever staged).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import PurePosixPath
from typing import BinaryIO, Iterator

D = Decimal
Y2K_PIVOT = 50


class DecodeError(ValueError):
    pass


class ControlTotalMismatch(ValueError):
    pass


@dataclass
class Field_:
    name: str
    pic: str
    length: int
    decimals: int
    signed: bool
    comp3: bool
    offset: int
    size: int
    conditions: dict[str, str] = field(default_factory=dict)
    binary: bool = False
    variant: str | None = None        # the REDEFINES item this field belongs to (an alternative layout)
    overlaid_by: list[str] = field(default_factory=list)   # alternatives that redefine this field's bytes


@dataclass
class RecordLayout:
    record_type: str | None
    fields: list[Field_]
    lrecl: int


_LINE = re.compile(
    r"^\s*(?P<level>\d{2})\s+(?P<name>[A-Z0-9-]+)"
    r"(?:\s+REDEFINES\s+(?P<redef>[A-Z0-9-]+))?"
    r"(?:\s+OCCURS\s+(?P<occurs>\d+)\s+TIMES)?"
    r"(?:\s+PIC(?:TURE)?\s+(?P<pic>[SX9V()0-9]+))?"
    r"(?:\s+(?:USAGE\s+(?:IS\s+)?)?(?P<comp>COMP-3|COMPUTATIONAL-3|PACKED-DECIMAL|COMP-[45]|COMPUTATIONAL-[45]"
    r"|COMP|COMPUTATIONAL|BINARY))?"
    r"(?:\s+OCCURS\s+(?P<occurs2>\d+)\s+TIMES)?(?:\s+VALUE\s+.+?)?\s*\.\s*$", re.I)
_L88 = re.compile(r"^\s*88\s+(?P<name>[A-Z0-9-]+)\s+VALUES?\s+'(?P<val>[^']*)'.*\.\s*$", re.I)
_OVER = {**{0xC0 + i: (i, 1) for i in range(10)}, **{0xD0 + i: (i, -1) for i in range(10)},
         **{0xF0 + i: (i, 1) for i in range(10)}}


def _pic_meta(pic: str) -> tuple[str, int, int, bool]:
    pic = pic.upper()
    m = re.match(r"^X(?:\((\d+)\))?$", pic) or re.match(r"^(X+)$", pic)
    if m:
        g = m.group(1)
        return ("X", int(g) if g and g.isdigit() else len(pic), 0, False)
    m = re.match(r"^(S?)9(?:\((\d+)\))?(9*)(?:V(9+|9\(\d+\)))?$", pic)
    if m:
        digits = (int(m.group(2)) if m.group(2) else 1) + len(m.group(3) or "")
        dec = 0
        if m.group(4):
            g = m.group(4)
            dec = int(g[2:-1]) if g.startswith("9(") else len(g)
        return ("9", digits, dec, m.group(1) == "S")
    raise DecodeError(f"unsupported PIC {pic!r}")


def _usage(comp: str | None) -> tuple[bool, bool]:
    """(packed, binary) from a USAGE clause."""
    u = (comp or "").upper()
    return u in ("COMP-3", "COMPUTATIONAL-3", "PACKED-DECIMAL"), u in (
        "COMP", "COMPUTATIONAL", "COMP-4", "COMPUTATIONAL-4", "COMP-5", "COMPUTATIONAL-5", "BINARY")


def _byte_size(kind: str, digits: int, dec: int, comp3: bool, binary: bool = False) -> int:
    if kind == "X":
        return digits
    if binary:
        total = digits + dec
        return 2 if total <= 4 else 4 if total <= 9 else 8
    return (digits + dec) // 2 + 1 if comp3 else digits + dec


def _strip_cobol(text: str) -> list[str]:
    out = []
    for ln in text.splitlines():
        if len(ln) > 6 and ln[6] in "*/":
            continue
        body = ln[7:72] if len(ln) > 7 and ln[:6].strip().isdigit() else ln
        if body.strip() and not body.lstrip().startswith("*"):
            out.append(body)
    return out


def parse_copybook(text: str) -> dict[str | None, RecordLayout]:
    """Per record type layouts. ``01 NAME-RECORD-<type>.`` groups give typed layouts; a single unnamed-type
    01 group gives the layout under key ``None``."""
    lines = _strip_cobol(text)
    blocks: list[tuple[str | None, list[str]]] = []
    for ln in lines:
        m = re.match(r"^\s*01\s+([A-Z0-9-]+)\s*\.\s*$", ln, re.I)
        if m:
            suffix = m.group(1).rsplit("-", 1)[-1]
            blocks.append((suffix if "-" in m.group(1) and suffix.isalnum() and len(suffix) <= 4 else None, []))
            continue
        if blocks:
            blocks[-1][1].append(ln)
    if not blocks:
        raise DecodeError("copybook has no 01-level record")
    if len(blocks) == 1:
        blocks = [(None, blocks[0][1])]
    layouts: dict[str | None, RecordLayout] = {}
    for rt, body in blocks:
        fields: list[Field_] = []
        spans: dict[str, tuple[int, int, list[int]]] = {}       # item -> (offset, size, field indexes)
        offset, i = 0, 0

        def add(nm: str, cm, off: int, variant: str | None) -> int:
            kind, n, dec, signed = _pic_meta(cm.group("pic"))
            packed, binary = _usage(cm.group("comp"))
            size = _byte_size(kind, n, dec, packed, binary)
            fields.append(Field_(nm, kind, n, dec, signed, packed, off, size, binary=binary, variant=variant))
            return size

        variant_stack: list[tuple[str, int]] = []                # (redefining item, level)
        while i < len(body):
            ln = body[i]
            m88 = _L88.match(ln)
            if m88:
                if fields:
                    fields[-1].conditions[m88.group("name")] = m88.group("val")
                i += 1
                continue
            m = _LINE.match(ln)
            if not m:
                raise DecodeError(f"unparseable copybook line: {ln.strip()!r}")
            level, name = int(m.group("level")), m.group("name").upper()
            while variant_stack and level <= variant_stack[-1][1]:
                variant_stack.pop()
            resume = None
            if m.group("redef"):
                target = m.group("redef").upper()
                if target not in spans:
                    raise DecodeError(f"{name} REDEFINES {target}, which is not defined before it")
                resume = offset
                offset = spans[target][0]
                variant_stack.append((name, level))
                for k in spans[target][2]:
                    fields[k].overlaid_by.append(name)
            variant = variant_stack[-1][0] if variant_stack else None
            first = len(fields)
            item_start = offset
            occurs = int(m.group("occurs") or m.group("occurs2") or 1)
            if m.group("pic") is None:                       # group item: expand children OCCURS times
                j, children = i + 1, []
                while j < len(body):
                    mm = _LINE.match(body[j])
                    if mm and int(mm.group("level")) <= level:
                        break
                    children.append(body[j])
                    j += 1
                for occ in range(1, occurs + 1):
                    for ch in children:
                        cm = _LINE.match(ch)
                        if not cm or cm.group("pic") is None:
                            continue
                        nm = cm.group("name").upper() if occ == 1 else f"{cm.group('name').upper()}#{occ}"
                        offset += add(nm, cm, offset, variant)
                i = j
            else:
                for occ in range(1, occurs + 1):
                    nm = name if occ == 1 else f"{name}#{occ}"
                    offset += add(nm, m, offset, variant)
                i += 1
            spans[name] = (item_start, offset - item_start, list(range(first, len(fields))))
            if resume is not None:          # a redefinition shares bytes: continue after the longer of the two
                offset = max(resume, offset)
                if m.group("pic") is not None or i >= len(body) or not _LINE.match(body[i]) or \
                        int(_LINE.match(body[i]).group("level")) <= level:
                    variant_stack = [v for v in variant_stack if v[0] != name]
        layouts[rt] = RecordLayout(rt, fields, max([f.offset + f.size for f in fields] or [0]))
    return layouts


# ------------------------------------------------------------------ field decoders
def dec_display(raw: bytes) -> str | None:
    if raw == b"\x00" * len(raw):
        return None
    return raw.decode("cp037").rstrip()


def dec_packed(raw: bytes, decimals: int) -> Decimal:
    nib = raw.hex().upper()
    sign, digits = nib[-1], nib[:-1]
    if sign not in ("C", "D", "F") or not digits.isdigit():
        raise DecodeError(f"bad packed decimal {nib!r}")
    v = D(digits).scaleb(-decimals)
    return -v if sign == "D" else v


def dec_binary(raw: bytes, decimals: int, signed: bool) -> Decimal:
    """COMP / COMP-4 / COMP-5 / BINARY: big-endian (mainframe byte order), two's complement when signed."""
    if len(raw) not in (2, 4, 8):
        raise DecodeError(f"binary field of {len(raw)} bytes")
    return D(int.from_bytes(raw, "big", signed=signed)).scaleb(-decimals)


def dec_zoned(raw: bytes, decimals: int, overpunch: bool) -> Decimal:
    if not raw:
        raise DecodeError("empty zoned field")
    if overpunch:
        last = raw[-1]
        if last not in _OVER:
            raise DecodeError(f"bad overpunch byte 0x{last:02X}")
        digit, sign = _OVER[last]
        body = raw[:-1].decode("cp037")
        if body and not body.isdigit():
            raise DecodeError(f"bad zoned digits {body!r}")
        v = D(body + str(digit)).scaleb(-decimals)
        return -v if sign < 0 else v
    s = raw.decode("cp037")
    if not s.isdigit():
        raise DecodeError(f"bad unsigned digits {s!r}")
    return D(s).scaleb(-decimals)


def dec_date(raw: bytes, fmt: str) -> str | None:
    s = raw.decode("cp037")
    if not s.isdigit():
        raise DecodeError(f"bad date digits {s!r}")
    if set(s) == {"0"}:
        return None
    if set(s) == {"9"}:
        return "9999-12-31"
    try:
        if fmt == "cyymmdd":
            return date((1900 if s[0] == "0" else 2000) + int(s[1:3]), int(s[3:5]), int(s[5:7])).isoformat()
        if fmt == "yymmdd":
            yy = int(s[0:2])
            return date((2000 if yy < Y2K_PIVOT else 1900) + yy, int(s[2:4]), int(s[4:6])).isoformat()
        if fmt == "yyyymmdd":
            return date(int(s[0:4]), int(s[4:6]), int(s[6:8])).isoformat()
        if fmt == "yyddd":
            yy = int(s[0:2])
            return (date((2000 if yy < Y2K_PIVOT else 1900) + yy, 1, 1) + timedelta(days=int(s[2:5]) - 1)).isoformat()
    except ValueError as exc:
        raise DecodeError(f"invalid date {s!r} ({fmt}): {exc}") from exc
    raise DecodeError(f"unknown date format {fmt}")


def _base(name: str) -> str:
    return name.split("#")[0]


def _decode_field(f: Field_, chunk: bytes, fmts: dict, over: set) -> object:
    if f.pic == "X":
        return dec_display(chunk)
    if _base(f.name) in fmts:
        return dec_date(chunk, fmts[_base(f.name)])
    if f.binary:
        return dec_binary(chunk, f.decimals, f.signed)
    if f.comp3:
        return dec_packed(chunk, f.decimals)
    return dec_zoned(chunk, f.decimals, f.signed or _base(f.name) in over)


def decode_record(raw: bytes, layout: RecordLayout, descriptor: dict) -> dict[str, object]:
    """Decode one record. Overlapping REDEFINES layouts: with a descriptor rule the matching layout decodes strictly
    and the others are empty; without one the original decodes strictly and an alternative only where valid."""
    from reasoning.deterministic.predicates import matches
    fmts = {k.upper(): v for k, v in (descriptor.get("date_formats") or {}).items()}
    over = {f.upper() for f in descriptor.get("overpunch_fields") or []}
    rules = {k.upper(): v for k, v in (descriptor.get("redefines") or {}).items()}
    rec: dict[str, object] = {}
    chosen: dict[str, bool] = {}

    def active(variant: str) -> bool | None:
        if variant not in rules:
            return None
        if variant not in chosen:
            chosen[variant] = matches(rules[variant], lambda n: None if rec.get(n.upper()) is None
                                      else str(rec.get(n.upper())))
        return chosen[variant]

    for f in layout.fields:
        chunk = raw[f.offset:f.offset + f.size]
        if f.variant is not None:
            state = active(f.variant)
            if state is False:
                rec[f.name] = None
                continue
            strict = state is True
        else:
            states = [active(v) for v in f.overlaid_by]
            if any(s is True for s in states):
                rec[f.name] = None         # an alternative layout owns these bytes in this record
                continue
            strict = True
        try:
            rec[f.name] = _decode_field(f, chunk, fmts, over)
        except DecodeError as exc:
            if strict:
                raise DecodeError(f"{f.name}: {exc}") from exc
            rec[f.name] = None             # an alternative whose bytes do not fit it in this record
    return rec


# ------------------------------------------------------------------ companion discovery + streaming decode
def _stem_variants(path: str) -> set[str]:
    stem = PurePosixPath(path).stem.upper()
    return {stem, stem.replace(".", "-"), stem.replace("-", "."), stem.replace(".", "_"), stem.replace("-", "_")}


def find_companions(dat_path: str, all_paths: list[str]) -> tuple[str | None, str | None]:
    """(copybook path, descriptor path) shipped alongside a ``.dat`` extract in the same upload."""
    folder = str(PurePosixPath(dat_path).parent)
    variants = _stem_variants(dat_path)
    cpy = desc = None
    for p in all_paths:
        pp = PurePosixPath(p)
        if str(pp.parent) != folder:
            continue
        if pp.suffix.lower() in (".cpy", ".copy") and pp.stem.upper() in variants:
            cpy = p
        if p.lower().endswith(".descriptor.json") and pp.name[:-len(".descriptor.json")].upper() in variants:
            desc = p
    if desc is None:
        for p in all_paths:
            if p.lower().endswith(".descriptor.json") and str(PurePosixPath(p).parent) == folder:
                d = PurePosixPath(p).name[:-len(".descriptor.json")].upper()
                if d in variants:
                    desc = p
    return cpy, desc


@dataclass
class DecodeResult:
    columns: list[str]
    records: int = 0
    rejected: int = 0
    controls: dict = field(default_factory=dict)
    quarantine: list[dict] = field(default_factory=list)     # first records that did not decode, with the reason


def decode_stream(stream: BinaryIO, copybook_text: str, descriptor: dict | None) -> tuple[list[str], Iterator[tuple[int, list[str]]], DecodeResult]:
    """Columns + a generator of ``(record_no, values)`` for detail records. The generator verifies the trailer
    control totals when it finishes and raises ``ControlTotalMismatch`` if the bytes disagree with them."""
    desc = dict(descriptor or {})
    layouts = parse_copybook(copybook_text)
    detail_type = desc.get("detail_type")
    if detail_type is None and None in layouts:
        detail = layouts[None]
    elif detail_type is not None and detail_type in layouts:
        detail = layouts[detail_type]
    elif len(layouts) == 1:
        detail = next(iter(layouts.values()))
    else:
        raise DecodeError("multi-record copybook needs a descriptor naming the detail record type")
    lrecl = int(desc.get("lrecl") or max(l.lrecl for l in layouts.values()))
    type_len = int(desc.get("type_len", 2 if detail.record_type else 0))
    trailer_type = desc.get("trailer_type", "99")
    hash_fields = {k.upper(): v.upper() for k, v in (desc.get("hash_fields") or {}).items()}
    type_field = detail.fields[0].name if (detail.record_type and detail.fields) else None
    columns = [f.name for f in detail.fields if f.name != "FILLER" and f.name != type_field]
    result = DecodeResult(columns=columns)

    def gen() -> Iterator[tuple[int, list[str]]]:
        sums = {k: D(0) for k in hash_fields}
        trailer: dict | None = None
        ordinal = 0
        while True:
            raw = stream.read(lrecl)
            if not raw:
                break
            ordinal += 1
            if len(raw) != lrecl:
                raise DecodeError(f"truncated record #{ordinal}: {len(raw)} of {lrecl} bytes")
            rt = raw[:type_len].decode("cp037") if type_len else None
            layout = detail if rt in (None, detail.record_type) else layouts.get(rt)
            if layout is None:
                result.rejected += 1
                continue
            if rt is not None and rt == trailer_type and layout is not detail:
                trailer = decode_record(raw, layout, desc)
                continue
            if layout is not detail:
                continue     # header or other record types are not data rows
            try:
                rec = decode_record(raw, layout, desc)
            except DecodeError as exc:
                result.rejected += 1
                if len(result.quarantine) < 200:
                    result.quarantine.append({"record": ordinal, "reason": str(exc)[:200]})
                continue
            result.records += 1
            for trl, src in hash_fields.items():
                v = rec.get(src)
                sums[trl] += v if isinstance(v, Decimal) else D(0)
            yield ordinal, ["" if rec.get(c) is None else str(rec.get(c)) for c in columns]
        if trailer is not None:
            count_field = desc.get("trailer_count_field") or next(
                (k for k in trailer if k.endswith("REC-COUNT") or k.endswith("COUNT")), None)
            expected_n = int(trailer.get(count_field) or 0) if count_field else None
            totals = {}
            ok = expected_n is None or expected_n == result.records
            for trl, actual in sums.items():
                exp = trailer.get(trl)
                exp_d = D(str(exp)).quantize(D("0.01")) if exp is not None else None
                act_d = actual.quantize(D("0.01"))
                totals[trl] = {"expected": str(exp_d), "actual": str(act_d)}
                ok = ok and exp_d == act_d
            result.controls = {"expected_records": expected_n, "actual_records": result.records,
                               "rejected_records": result.rejected, "totals": totals, "reconciled": ok}
            if not ok:
                raise ControlTotalMismatch(f"trailer control totals do not reconcile: {result.controls}")
        else:
            result.controls = {"actual_records": result.records, "rejected_records": result.rejected,
                               "reconciled": None, "note": "no trailer record - control totals not verifiable"}
    return columns, gen(), result


def load_descriptor(data: bytes) -> dict:
    try:
        d = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DecodeError(f"invalid descriptor: {exc}") from exc
    if not isinstance(d, dict):
        raise DecodeError("descriptor must be a JSON object")
    return d
