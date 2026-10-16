"""Exact, deterministic transformations and value comparison (mapping, code generation, reconciliation)."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from schemas.mapping import TransformationSpec

TRANSFORM_VERSION = "transform-1.1"


class TransformError(ValueError):
    pass


def to_decimal(v: str | None) -> Decimal | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return Decimal(str(v).strip())
    except InvalidOperation:
        raise TransformError(f"not a decimal: {v!r}") from None


def slash_order() -> str:
    """``dmy`` (default) or ``mdy``: how the project's sources write 01/02/2026 (``heuristics.dates.slash_order``)."""
    from config.settings import active_settings
    try:
        order = (active_settings().heuristics.get("dates") or {}).get("slash_order", "dmy")
    except Exception:
        order = "dmy"
    return "mdy" if str(order).lower() == "mdy" else "dmy"


def parse_date(v: str, order: str | None = None) -> date:
    s = v.strip()
    first = "%m/%d/%Y" if (order or slash_order()) == "mdy" else "%d/%m/%Y"
    dashed = "%m-%d-%Y" if first.startswith("%m") else "%d-%m-%Y"
    dotted = "%m.%d.%Y" if first.startswith("%m") else "%d.%m.%Y"
    for fmt in ("%Y-%m-%d", "%Y%m%d", first, dashed, dotted, "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise TransformError(f"not a date: {v!r}")


def apply(spec: TransformationSpec, value: str | None) -> str | None:
    """Apply a certified transformation to one source value. Raises TransformError on bad input."""
    if value is None:
        return None
    kind = spec.kind
    p = spec.params
    if kind == "direct":
        return value
    if kind == "cast":
        target = p.get("to")
        if value.strip() == "":
            return None
        if target == "decimal":
            d = to_decimal(value)
            return None if d is None else format(d.quantize(Decimal(1).scaleb(-p["scale"])) if p.get("scale") is not None else d, "f")
        if target == "integer":
            d = to_decimal(value)
            if d is None:
                return None
            if d != d.to_integral():
                raise TransformError(f"{value!r} is not an integer")
            return str(int(d))
        if target == "string":
            return value
        raise TransformError(f"unsupported cast to {target}")
    if kind == "code_map":
        mapping: dict = p.get("map", {})
        if value in mapping:
            return mapping[value]
        if p.get("strict", True):
            raise TransformError(f"code {value!r} has no certified mapping")
        return value
    if kind == "date_format":
        if value.strip() in p.get("null_sentinels", ["00000000", ""]):
            return None
        return parse_date(value).isoformat()
    raise TransformError(f"unknown transformation kind {kind}")


def values_equal(expected: str | None, actual: str | None, data_type: str | None) -> tuple[bool, Decimal | None]:
    """Exact comparison by type. Returns (equal, numeric delta actual - expected when numeric)."""
    if expected is None or actual is None:
        return (expected is None and (actual is None or actual.strip() == "")), None
    if data_type in ("decimal", "integer"):
        e, a = to_decimal(expected), to_decimal(actual)
        if e is None or a is None:
            return e == a, None
        return e == a, a - e
    if data_type == "boolean":
        norm = {"true": True, "t": True, "1": True, "y": True, "false": False, "f": False, "0": False, "n": False}
        return norm.get(expected.strip().lower()) == norm.get(actual.strip().lower()), None
    if data_type == "date":
        try:
            return parse_date(expected) == parse_date(actual), None
        except TransformError:
            return expected.strip() == actual.strip(), None
    return expected.strip() == actual.strip(), None


def infer_type(values: list[str]) -> str:
    vals = [v for v in values if v is not None and v.strip() != ""]
    if not vals:
        return "string"
    if all(re.fullmatch(r"-?\d+", v.strip()) for v in vals):
        return "integer"
    if all(re.fullmatch(r"-?\d+\.\d+|-?\d+", v.strip()) for v in vals):
        return "decimal"
    if all(v.strip().lower() in ("true", "false", "y", "n", "t", "f") for v in vals):
        return "boolean"
    if all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v.strip()) for v in vals):
        return "date"
    return "string"
