"""Two-tier reconciliation tolerance: an absolute floor, and a relative allowance in basis points of the
source value - whichever is larger. A difference inside it is rounding, not a break.

Tolerances are engineering settings per project (``heuristics.reconciliation.tolerances``): by target field
(``ENTITY.FIELD`` or ``FIELD``) first, then by data type, else exact. Every break records the tolerance
that was applied, so a reviewer can see why it was raised.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

BP = Decimal(10000)


@dataclass(frozen=True)
class Tolerance:
    absolute_floor: Decimal = Decimal(0)
    relative_bp: Decimal = Decimal(0)

    @property
    def exact(self) -> bool:
        return self.absolute_floor == 0 and self.relative_bp == 0

    def allowance(self, expected: Decimal) -> Decimal:
        return max(self.absolute_floor, abs(expected) * self.relative_bp / BP)

    def within(self, expected: Decimal, delta: Decimal) -> bool:
        return abs(delta) <= self.allowance(expected)

    def as_dict(self) -> dict:
        return {"absolute_floor": format(self.absolute_floor, "f"), "relative_bp": format(self.relative_bp, "f")}


def _tol(spec: dict | None) -> Tolerance | None:
    if not spec:
        return None
    return Tolerance(Decimal(str(spec.get("absolute_floor", "0"))), Decimal(str(spec.get("relative_bp", "0"))))


def tolerance_for(cfg: dict, entity: str, field: str, data_type: str | None) -> Tolerance:
    tol = cfg.get("tolerances") or {}
    fields = {k.upper(): v for k, v in (tol.get("fields") or {}).items()}
    return (_tol(fields.get(f"{entity}.{field}".upper())) or _tol(fields.get(field.upper()))
            or _tol((tol.get("by_type") or {}).get(data_type or "string")) or Tolerance())
