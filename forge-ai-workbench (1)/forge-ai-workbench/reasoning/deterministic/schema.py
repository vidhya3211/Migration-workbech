"""Deterministic schema comparison: type/length/nullability/domain compatibility of two attributes."""
from __future__ import annotations

from dataclasses import dataclass

from schemas.mapping import DictionaryEntry

COMPATIBLE = {
    ("string", "string"), ("integer", "integer"), ("decimal", "decimal"), ("date", "date"), ("boolean", "boolean"),
    ("integer", "decimal"), ("string", "date"), ("string", "boolean"), ("string", "integer"), ("string", "decimal"),
    ("date", "string"), ("integer", "string"), ("decimal", "string"),
}
LOSSY = {("decimal", "integer"), ("string", "integer"), ("string", "decimal"), ("string", "date"), ("string", "boolean")}


@dataclass
class SchemaCheck:
    check: str
    passed: bool
    severity: str
    detail: str


def compare(src: DictionaryEntry, tgt: DictionaryEntry) -> list[SchemaCheck]:
    out: list[SchemaCheck] = []
    st, tt = src.data_type or "string", tgt.data_type or "string"
    compat = (st, tt) in COMPATIBLE
    out.append(SchemaCheck("type_compatible", compat, "block" if not compat else "info",
                           f"{st} -> {tt}" + (" (needs a checked conversion)" if (st, tt) in LOSSY else "")))
    if src.length and tgt.length and st == tt == "string":
        ok = src.length <= tgt.length
        out.append(SchemaCheck("length", ok, "block" if not ok else "info",
                               f"source length {src.length} vs target {tgt.length}"))
    if st in ("decimal", "integer") and tt in ("decimal", "integer") and src.length and tgt.length:
        s_int = src.length - (src.scale or 0)
        t_int = tgt.length - (tgt.scale or 0)
        ok = s_int <= t_int and (src.scale or 0) <= (tgt.scale or 0) + (0 if tt == "decimal" else 0)
        out.append(SchemaCheck("precision", ok, "warn" if not ok else "info",
                               f"source ({src.length},{src.scale or 0}) vs target ({tgt.length},{tgt.scale or 0})"))
    if src.nullable and not tgt.nullable:
        out.append(SchemaCheck("nullability", False, "warn",
                               "source allows NULL but target is NOT NULL - a default or rejection rule is needed"))
    if src.allowed_values and tgt.allowed_values:
        missing = sorted(set(src.allowed_values) - set(tgt.allowed_values))
        out.append(SchemaCheck("domain", not missing, "warn" if missing else "info",
                               f"source codes not allowed in target: {missing}" if missing else "code domains align"))
    return out
