"""Deterministic SQL DDL reader (Oracle first; tolerant of PostgreSQL/MySQL target schemas).

Recovers tables, columns (type, length, scale, nullability, defaults), CHECK / PRIMARY KEY / FOREIGN KEY /
UNIQUE constraints, views, triggers and COMMENT ON COLUMN - each with exact line ranges.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

TYPE_MAP = [
    (re.compile(r"^(VARCHAR2?|NVARCHAR2?|CHAR|NCHAR|TEXT|CLOB|STRING|CHARACTER VARYING)", re.I), "string"),
    (re.compile(r"^(DATE|TIMESTAMP|DATETIME)", re.I), "date"),
    (re.compile(r"^(BOOLEAN|BOOL|BIT|TINYINT\(1\))", re.I), "boolean"),
    (re.compile(r"^(INTEGER|INT|SMALLINT|BIGINT|TINYINT|SERIAL|BIGSERIAL|PLS_INTEGER)", re.I), "integer"),
    (re.compile(r"^(NUMBER|NUMERIC|DECIMAL|FLOAT|DOUBLE|REAL|BINARY_DOUBLE|MONEY)", re.I), "decimal"),
]


@dataclass
class Column:
    name: str
    raw_type: str
    data_type: str
    length: int | None
    scale: int | None
    nullable: bool
    default: str | None
    line_start: int
    line_end: int
    checks: list[str] = field(default_factory=list)
    comment: str | None = None


@dataclass
class Constraint:
    name: str | None
    kind: str             # check | primary_key | foreign_key | unique | not_null
    expression: str
    columns: list[str]
    line_start: int
    line_end: int


@dataclass
class Table:
    schema: str | None
    name: str
    columns: list[Column]
    constraints: list[Constraint]
    line_start: int
    line_end: int


@dataclass
class SchemaObject:
    kind: str             # view | trigger
    schema: str | None
    name: str
    detail: str
    line_start: int
    line_end: int


@dataclass
class DdlModel:
    tables: list[Table] = field(default_factory=list)
    objects: list[SchemaObject] = field(default_factory=list)


def _strip_comments(text: str) -> str:
    def blank(m: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", m.group())
    text = re.sub(r"/\*.*?\*/", blank, text, flags=re.S)
    return re.sub(r"--[^\n]*", blank, text)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def normalise_type(raw: str) -> tuple[str, int | None, int | None]:
    data_type = next((t for pat, t in TYPE_MAP if pat.match(raw.strip())), "string")
    m = re.search(r"\(\s*(\d+)(?:\s*(?:BYTE|CHAR))?\s*(?:,\s*(-?\d+))?\s*\)", raw, re.I)
    length = int(m.group(1)) if m else None
    scale = int(m.group(2)) if m and m.group(2) else None
    if data_type == "decimal" and re.match(r"^NUMBER", raw, re.I) and m and (scale in (None, 0)):
        data_type = "integer"
    return data_type, length, scale


def _split_top(body: str, offset: int) -> list[tuple[str, int]]:
    """Split a CREATE TABLE body on top-level commas, keeping each part's absolute start offset."""
    parts: list[tuple[str, int]] = []
    depth, start, quote = 0, 0, False
    for i, ch in enumerate(body):
        if ch == "'":
            quote = not quote
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")":
            depth -= 1
        elif not quote and ch == "," and depth == 0:
            parts.append((body[start:i], offset + start))
            start = i + 1
    parts.append((body[start:], offset + start))
    return [(p, o + (len(p) - len(p.lstrip()))) for p, o in parts if p.strip()]


_IDENT = r'"?[A-Za-z_][A-Za-z0-9_$#]*"?'
_CREATE_TABLE = re.compile(rf"\bCREATE\s+(?:GLOBAL\s+TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:({_IDENT})\.)?({_IDENT})\s*\(", re.I)
_CREATE_OBJ = re.compile(rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(VIEW|TRIGGER)\s+(?:({_IDENT})\.)?({_IDENT})([^;]*?)(?:;|$)", re.I | re.S)
_COMMENT = re.compile(rf"\bCOMMENT\s+ON\s+COLUMN\s+(?:({_IDENT})\.)?({_IDENT})\.({_IDENT})\s+IS\s+'((?:[^']|'')*)'", re.I)


def _unq(s: str | None) -> str | None:
    return s.strip('"') if s else s


def parse(text: str) -> DdlModel:
    src = _strip_comments(text)
    model = DdlModel()
    for m in _CREATE_TABLE.finditer(src):
        start = m.end()
        depth, i = 1, start
        while i < len(src) and depth:
            depth += {"(": 1, ")": -1}.get(src[i], 0)
            i += 1
        body = src[start:i - 1]
        table = Table(_unq(m.group(1)), _unq(m.group(2)), [], [], _line_of(src, m.start()), _line_of(src, i))
        for part, off in _split_top(body, start):
            p = part.strip()
            ls, le = _line_of(src, off), _line_of(src, off + len(part.rstrip()))
            pu = p.upper()
            cname_m = re.match(rf"CONSTRAINT\s+({_IDENT})\s+(.*)$", p, re.I | re.S)
            cname, rest = (_unq(cname_m.group(1)), cname_m.group(2)) if cname_m else (None, p)
            ru = rest.upper()
            if ru.startswith("PRIMARY KEY") or ru.startswith("UNIQUE") or ru.startswith("FOREIGN KEY") or ru.startswith("CHECK"):
                kind = ("primary_key" if ru.startswith("PRIMARY") else "unique" if ru.startswith("UNIQUE")
                        else "foreign_key" if ru.startswith("FOREIGN") else "check")
                pm = re.search(r"\(([^)]*)\)", rest)
                inner = re.sub(r"'(?:[^']|'')*'", " ", pm.group(1)) if pm else ""
                cols = [_unq(c) for c in re.findall(_IDENT, inner)
                        if c.upper() not in ("IN", "AND", "OR", "NOT", "NULL", "BETWEEN", "IS")]
                table.constraints.append(Constraint(cname, kind, " ".join(rest.split()), cols, ls, le))
                continue
            if pu.startswith(("CONSTRAINT", "INDEX", "KEY ")):
                continue
            cm = re.match(rf"({_IDENT})\s+(.+)$", p, re.S)
            if not cm:
                continue
            name, spec = _unq(cm.group(1)), cm.group(2)
            tm = re.match(r"([A-Za-z_][A-Za-z0-9_ ]*?(?:\([^)]*\))?)(?=\s|$)", spec)
            raw_type = tm.group(1).strip() if tm else spec.split()[0]
            data_type, length, scale = normalise_type(raw_type)
            dm = re.search(r"\bDEFAULT\s+('(?:[^']|'')*'|-?\d+(?:\.\d+)?|\w+(?:\(\))?)", spec, re.I)
            col = Column(name, raw_type, data_type, length, scale,
                         nullable=not re.search(r"\bNOT\s+NULL\b|\bPRIMARY\s+KEY\b", spec, re.I),
                         default=dm.group(1) if dm else None, line_start=ls, line_end=le)
            for ck in re.finditer(r"\bCHECK\s*\((.*)\)", spec, re.I | re.S):
                col.checks.append(" ".join(ck.group(1).split()))
            if re.search(r"\bPRIMARY\s+KEY\b", spec, re.I):
                table.constraints.append(Constraint(None, "primary_key", f"PRIMARY KEY ({name})", [name], ls, le))
            table.columns.append(col)
        model.tables.append(table)
    for m in _CREATE_OBJ.finditer(src):
        detail = " ".join(m.group(4).split())[:300]
        model.objects.append(SchemaObject(m.group(1).lower(), _unq(m.group(2)), _unq(m.group(3)), detail,
                                          _line_of(src, m.start()), _line_of(src, m.end())))
    tables = {t.name.upper(): t for t in model.tables}
    for m in _COMMENT.finditer(src):
        t = tables.get((_unq(m.group(2)) or "").upper())
        if t:
            for c in t.columns:
                if c.name.upper() == (_unq(m.group(3)) or "").upper():
                    c.comment = m.group(4).replace("''", "'")
    return model


def allowed_values(check: str, column: str) -> list[str]:
    """``STATUS IN ('A','L','P')`` -> ['A', 'L', 'P'] for the named column."""
    m = re.search(rf"\b{re.escape(column)}\b\s+IN\s*\(([^)]*)\)", check, re.I)
    if not m:
        return []
    return [v.strip().strip("'") for v in m.group(1).split(",") if v.strip()]
