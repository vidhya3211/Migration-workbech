"""Record conditions written in configuration, evaluated deterministically.

Projects describe their business facts as data, never as code: which records are out of migration scope,
which source fields carry a customer right, which flags mark a vulnerable customer, which states mean "in
flight at cutover". One small, explainable condition language serves all of them::

    {field: STATUS, in: [L, X]}              {field: GAR_IND, equals: "Y"}
    {field: PROT_RET_AGE, gt: 0}             {field: LAST_PREM_DATE, before: "2019-01-01"}
    {field: CLAIM_STATE, not_in: ["", N]}    {field: MVR_FREE_DATE, present: true}
    {all: [...]} / {any: [...]}              (combinations)

Comparisons are case-insensitive for text, numeric for ``gt``/``lt``/``gte``/``lte`` and ISO-date for
``before``/``after``. A field missing from the record makes the condition false (never an error).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping

from reasoning.deterministic.transform import TransformError, parse_date

Getter = Callable[[str], "str | None"]


class ConditionError(ValueError):
    pass


def _num(v: str | None) -> Decimal | None:
    try:
        return Decimal(str(v).strip().replace(",", "")) if v not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _date(v: str | None):
    try:
        return parse_date(str(v)) if v not in (None, "") else None
    except TransformError:
        return None


def _norm(v) -> str:
    return str(v).strip().upper()


def fields_of(cond: Mapping) -> set[str]:
    if "all" in cond or "any" in cond:
        return {f for c in cond.get("all", cond.get("any", [])) for f in fields_of(c)}
    return {str(cond["field"]).upper()} if "field" in cond else set()


def matches(cond: Mapping, get: Getter) -> bool:
    if "all" in cond:
        return all(matches(c, get) for c in cond["all"])
    if "any" in cond:
        return any(matches(c, get) for c in cond["any"])
    if "field" not in cond:
        raise ConditionError(f"condition without a field: {dict(cond)}")
    raw = get(str(cond["field"]))
    value = None if raw is None else str(raw).strip()
    if "present" in cond:
        return bool(value) == bool(cond["present"])
    if value is None:
        return False
    if "equals" in cond:
        return _norm(value) == _norm(cond["equals"])
    if "in" in cond:
        return _norm(value) in {_norm(x) for x in cond["in"]}
    if "not_in" in cond:
        return _norm(value) not in {_norm(x) for x in cond["not_in"]}
    for op in ("gt", "gte", "lt", "lte"):
        if op in cond:
            a, b = _num(value), _num(cond[op])
            if a is None or b is None:
                return False
            return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    for op in ("before", "after"):
        if op in cond:
            a, b = _date(value), _date(cond[op])
            if a is None or b is None:
                return False
            return a < b if op == "before" else a > b
    if "starts_with" in cond:
        return _norm(value).startswith(_norm(cond["starts_with"]))
    raise ConditionError(f"unknown condition: {dict(cond)}")


def describe(cond: Mapping) -> str:
    if "all" in cond:
        return " and ".join(describe(c) for c in cond["all"])
    if "any" in cond:
        return " or ".join(describe(c) for c in cond["any"])
    ops = {k: v for k, v in cond.items() if k not in ("field", "reason", "name")}
    return f"{cond.get('field')} " + ", ".join(f"{k} {v}" for k, v in ops.items())


def row_getter(header: list[str], row: list[str]) -> Getter:
    """Case-insensitive column lookup over a staged row."""
    index = {h.upper(): i for i, h in enumerate(header)}

    def get(name: str) -> str | None:
        i = index.get(name.upper())
        return row[i] if i is not None and i < len(row) else None
    return get
