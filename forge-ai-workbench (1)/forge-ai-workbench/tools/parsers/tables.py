"""Rate / lookup tables lifted from source code - business values that live in data structures, not statements.

COBOL: the classic idiom is a group of ``FILLER ... VALUE`` items redefined by an ``OCCURS`` table::

    01  RATE-VALUES.
        05  FILLER  PIC X(6) VALUE 'A01150'.        <- key 'A01', rate 1.50
        05  FILLER  PIC X(6) VALUE 'B02175'.
    01  RATE-TABLE REDEFINES RATE-VALUES.
        05  RATE-ENTRY OCCURS 2 TIMES.
            10  RATE-CODE  PIC X(3).
            10  RATE-PCT   PIC 9V99.

An elementary ``OCCURS`` item gives a table indexed by position (1..n); a group entry whose first subfield is
alphanumeric gives a table keyed by that subfield, valued by its last numeric subfield (implied decimals
applied). PHP: keyed or positional array constants / properties (``const RATES = ['A' => 1.15, ...]``,
``private $rates = array(...)``); positional arrays are indexed from 1 so they compare with COBOL tables.

Each table carries its source lines; entries are text, numeric entries normalised (``1.150`` -> ``1.15``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from tools.parsers.cobol import DataItem


@dataclass
class Table:
    name: str
    entries: dict[str, str]
    index_by: str                    # position | key
    start_line: int
    end_line: int
    value_field: str | None = None
    notes: list[str] = field(default_factory=list)


def _norm(v: str) -> str:
    s = v.strip().strip("'\"")
    try:
        d = Decimal(s)
        return format(d.normalize(), "f") if d == d.to_integral_value() or "." in s else s
    except (InvalidOperation, ValueError):
        return s


def _pic_width(pic: str | None) -> tuple[int, int, bool]:
    """(display width, decimals, numeric) of a PIC string."""
    if not pic:
        return 0, 0, False
    p = pic.upper().rstrip(".")
    expanded = re.sub(r"([X9AZ])\((\d+)\)", lambda m: m.group(1) * int(m.group(2)), p)
    numeric = "X" not in expanded and "A" not in expanded
    before, _, after = expanded.partition("V")
    width = sum(1 for c in expanded if c in "X9AZ")
    return width, sum(1 for c in after if c in "9Z"), numeric


def cobol_tables(items: list[DataItem]) -> list[Table]:
    by_parent: dict[str | None, list[DataItem]] = {}
    for it in items:
        if it.level != 88:
            by_parent.setdefault(it.parent, []).append(it)
    out = []
    for r in (it for it in items if it.redefines):
        source = next((g for g in items if g.name.upper() == r.redefines.upper() and g.level == r.level), None)
        if source is None:
            continue
        fillers = [c for c in by_parent.get(source.name, []) if c.value is not None]
        if not fillers:
            continue
        entry = next((c for c in _descendants(r, by_parent) if c.occurs), None)
        if entry is None:
            continue
        values = [f.value for f in fillers]
        subs = [c for c in by_parent.get(entry.name, []) if c.pic]
        lo, hi = min(f.start_line for f in fillers), max(r.end_line, *(s.end_line for s in subs), entry.end_line)
        if entry.pic:                                    # elementary: position -> value
            _, decs, numeric = _pic_width(entry.pic)
            out.append(Table(entry.name, {str(i + 1): _implied(v, decs, numeric) for i, v in enumerate(values)},
                             "position", lo, hi, entry.name))
            continue
        widths = [_pic_width(s.pic) for s in subs]
        if len(subs) < 2 or widths[0][2] or not any(w[2] for w in widths):
            continue                                     # needs an alphanumeric key and a numeric value
        vi = max(i for i, w in enumerate(widths) if w[2])
        entries, bad = {}, 0
        for v in values:
            raw = v.strip("'\"")
            if len(raw) != sum(w[0] for w in widths):
                bad += 1
                continue
            parts, pos = [], 0
            for w, _, _ in widths:
                parts.append(raw[pos:pos + w])
                pos += w
            entries[parts[0].strip()] = _implied(parts[vi], widths[vi][1], True)
        if entries:
            t = Table(subs[vi].name, entries, "key", lo, hi, subs[vi].name)
            if bad:
                t.notes.append(f"{bad} VALUE literal(s) did not match the entry layout and were not read")
            out.append(t)
    return out


def _descendants(item: DataItem, by_parent: dict[str | None, list[DataItem]]) -> list[DataItem]:
    out, stack = [], list(by_parent.get(item.name, []))
    while stack:
        c = stack.pop(0)
        out.append(c)
        stack.extend(by_parent.get(c.name, []))
    return out


def _implied(v: str, decimals: int, numeric: bool) -> str:
    s = v.strip().strip("'\"")
    if not numeric:
        return s
    if "." in s or not decimals:
        return _norm(s)
    try:
        return format(Decimal(s).scaleb(-decimals).normalize(), "f")
    except (InvalidOperation, ValueError):
        return s


_PHP_DECL = re.compile(r"(?:\b(?:const|define)\s+|\$)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\[|array\s*\()", re.I)
_PHP_ENTRY = re.compile(r"""^\s*(?:(['"]?)([\w.\-]+)\1\s*=>\s*)?(-?[0-9]+(?:\.[0-9]+)?|'[^']*'|"[^"]*")\s*$""")


def php_tables(text: str) -> list[Table]:
    out = []
    for m in _PHP_DECL.finditer(text):
        open_ch = m.group(2)[0]
        close_ch = "]" if open_ch == "[" else ")"
        start = m.end()
        depth, i = 1, start
        while i < len(text) and depth:
            depth += {open_ch: 1, close_ch: -1}.get(text[i], 0)
            i += 1
        body = text[start:i - 1]
        if "[" in body or "array(" in body.lower():
            continue                                    # nested arrays are not rate tables
        entries, positional = {}, 0
        ok = True
        for part in [p for p in body.split(",") if p.strip()]:
            em = _PHP_ENTRY.match(part)
            if not em:
                ok = False
                break
            if em.group(2) is None:
                positional += 1
                entries[str(positional)] = _norm(em.group(3))
            else:
                entries[em.group(2)] = _norm(em.group(3))
        if not ok or len(entries) < 2 or not any(_is_num(v) for v in entries.values()):
            continue
        first = text.count("\n", 0, m.start()) + 1
        last = text.count("\n", 0, i) + 1
        out.append(Table(m.group(1), entries, "position" if positional == len(entries) else "key", first, last))
    return out


def _is_num(v: str) -> bool:
    try:
        Decimal(v)
        return True
    except (InvalidOperation, ValueError):
        return False
