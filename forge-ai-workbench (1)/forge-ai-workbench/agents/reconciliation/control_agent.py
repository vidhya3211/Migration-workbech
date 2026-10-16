"""Control reconciler (T3): the checks a record-by-record reconciliation cannot make.

Three kinds, each project configuration under ``heuristics.reconciliation`` (all optional):

* ``derived`` - a value the source does not store but implies: the source value at each date the target struck
  its value, computed as sum(quantity x latest price on or before that date) from the source holdings and price
  history (e.g. a fund value from units and unit prices). Compared with the loaded value under the project's
  tolerance, so a day-two strike is never compared with a day-one price.
* ``cross_target`` - two target systems that must agree with each other (e.g. a policy held on two retained
  platforms, kept aligned by a feed). A difference is drift in the feed - a break neither system can see alone.
* ``controls`` - control totals: the sum of one ledger (optionally filtered and grouped) against another or a
  stated balance (e.g. client-money ledger vs bank statement, unit register vs custodian holding per fund).
  A regime label (e.g. CASS) travels with the result.

Every result is a reconciliation break with its evidence (streamed, guarded and bulk-saved like the main
reconciliation), plus a summary on the run. Mechanical and deterministic: T3.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Callable

from agents.base import AgentOutput, BaseAgent
from agents.data_quality.extracts import Extract, load_extracts
from blackboard.identity import agent_identity, register_agent
from reasoning.deterministic.predicates import describe, fields_of, matches, row_getter
from reasoning.deterministic.tolerance import Tolerance, tolerance_for
from reasoning.deterministic.transform import slash_order
from schemas.common import ReasoningMode, Tier, stable_id
from schemas.mapping import DictionaryEntry
from schemas.recon import ReconciliationBreak
from schemas.signals import SignalType
from schemas.signals import SignalType as S

Sink = Callable[[list[ReconciliationBreak]], None]


def _dec(v) -> Decimal | None:
    try:
        return Decimal(str(v).strip().replace(",", "")) if v not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


@register_agent
class ControlReconcilerAgent(BaseAgent):
    IDENTITY = agent_identity("control_reconciler_agent", Tier.T3, "reconciliation",
                              "Derived values, cross-target drift and control totals",
                              {S.RECON_BREAKS}, tools={"evidence.read"}, proposes={"reconciliation_break"})

    def run(self, dictionary: list[DictionaryEntry], sink: Sink | None = None, **_: object) -> AgentOutput:
        cfg = self.services.settings.heuristics.get("reconciliation") or {}
        extracts = load_extracts(self.services, self.run_id, "source", dictionary) + \
            load_extracts(self.services, self.run_id, "target", dictionary)
        self._buf: list[ReconciliationBreak] = []
        self._collected: list[ReconciliationBreak] = []
        self._sink = sink
        summary = {"derived": [self._derived(spec, cfg, extracts) for spec in cfg.get("derived") or []],
                   "cross_target": [self._cross_target(spec, cfg, extracts) for spec in cfg.get("cross_target") or []],
                   "controls": [self._control(spec, extracts) for spec in cfg.get("controls") or []]}
        self._flush()
        self.note_mode(ReasoningMode.DETERMINISTIC)
        failed = [c for c in summary["controls"] if c.get("reconciled") is False]     # skipped is not failed
        breaks = sum(x.get("breaks", 0) for k in ("derived", "cross_target") for x in summary[k]) + \
            sum(len(c.get("mismatches", [])) for c in summary["controls"])
        self.signal(SignalType.RECON_BREAKS,
                    f"control reconciliation: {breaks} break(s); "
                    + ", ".join(f"{c['name']} {'reconciled' if c.get('reconciled') else 'BREAK'}"
                                for c in summary["controls"]) if summary["controls"] else
                    f"control reconciliation: {breaks} break(s)",
                    data=summary, item_refs=[b.break_id for b in self._collected][:200])
        return AgentOutput(items={"breaks": self._collected, "controls": [summary]},
                           summary={"control_failures": len(failed)})

    # ------------------------------------------------------------------ plumbing
    def _emit(self, b: ReconciliationBreak) -> None:
        self._buf.append(b)
        if len(self._buf) >= self.services.settings.scale.bulk_batch_size:
            self._flush()

    def _flush(self) -> None:
        if self._buf:
            (self._sink or self._collected.extend)(list(self._buf))
            if self._sink is not None:
                self._collected.extend(self._buf[:50])
            self._buf.clear()

    @staticmethod
    def _pick(sel: dict, extracts: list[Extract]) -> Extract | None:
        for ex in extracts:
            if sel.get("entity") and ex.entity and ex.entity.upper() == str(sel["entity"]).upper():
                return ex
        for ex in extracts:
            if sel.get("path_contains") and str(sel["path_contains"]).lower() in ex.artefact.path.lower():
                return ex
        return None

    @staticmethod
    def _col(ex: Extract, name: str) -> str:
        col = next((h for h in ex.header if h.upper() == str(name).upper()), None)
        if col is None:
            raise KeyError(f"{name} not in {ex.artefact.path}")
        return col

    def _break(self, entity: str, key: str, field: str, kind: str, sv, expected, tv, delta, evidence,
               *, eff: str | None = None, tolerance: dict | None = None, note: str | None = None) -> ReconciliationBreak:
        return ReconciliationBreak(
            break_id=stable_id("BRK", self.run_id, entity, key, field, kind, eff or ""), run_id=self.run_id,
            record_key=key, entity=entity, field=field, break_type=kind, source_value=sv, expected_value=expected,
            target_value=tv, delta=None if delta is None else format(delta, "f"), tolerance=tolerance,
            effective_date=eff, note=note, evidence=evidence)

    # ------------------------------------------------------------------ derived values
    def _derived(self, spec: dict, cfg: dict, extracts: list[Extract]) -> dict:
        name = spec.get("name") or "derived value"
        try:
            tx, hx, px = (self._pick(spec[k], extracts) for k in ("target", "holdings", "prices"))
            if not (tx and hx and px):
                return {"name": name, "skipped": "target, holdings or prices extract not found"}
            t, h, p = spec["target"], spec["holdings"], spec["prices"]
            tcols = [self._col(tx, t[k]) for k in ("key", "date", "field")]
            hcols = [self._col(hx, h[k]) for k in ("key", "instrument", "quantity")]
            pcols = [self._col(px, p[k]) for k in ("instrument", "date", "price")]
            hdate = self._col(hx, h["date"]) if h.get("date") else None      # dated holdings (units as of a date)
        except KeyError as exc:
            return {"name": name, "skipped": str(exc)}
        decimals = int(spec.get("decimals", 2))
        q = Decimal(1).scaleb(-decimals)
        tol = tolerance_for(cfg, tx.entity or "", tcols[2], "decimal")
        compared = breaks = unpriced = 0
        rows = self.services.staging.derived_values(
            (tx.artefact.artefact_id, *[tx.idx(c) for c in tcols]), (hx.artefact.artefact_id, *[hx.idx(c) for c in hcols]),
            (px.artefact.artefact_id, *[px.idx(c) for c in pcols]), slash_order(),
            hx.idx(hdate) if hdate else None)
        for line, key, dt, loaded, derived, n_hold, n_priced in rows:
            if n_hold == 0:
                continue                     # no source holding: not a derived value for this record
            ev = [tx.evidence(line, tx.row(line) or [], tcols[2])]
            if n_priced < n_hold:
                unpriced += 1
                self._emit(self._break(tx.entity or name, key, tcols[2], "derived_value_unpriced", None, None, loaded,
                                       None, ev, eff=dt, note=f"{n_hold - n_priced} of {n_hold} holding(s) have no "
                                                              f"price on or before {dt}"))
                continue
            compared += 1
            exp = Decimal(derived).quantize(q)
            act = _dec(loaded)
            delta = None if act is None else act - exp
            if act is None or (delta != 0 and not tol.within(exp, delta)):
                breaks += 1
                self._emit(self._break(tx.entity or name, key, tcols[2], "derived_value_mismatch", None, format(exp, "f"),
                                       loaded, delta, ev, eff=dt, tolerance=None if tol.exact else tol.as_dict(),
                                       note=f"{name}: sum of quantity x price as of {dt}"))
        return {"name": name, "compared": compared, "breaks": breaks, "unpriced": unpriced}

    # ------------------------------------------------------------------ cross-target drift
    def _cross_target(self, spec: dict, cfg: dict, extracts: list[Extract]) -> dict:
        name = spec.get("name") or "cross-target"
        lx, rx = self._pick(spec.get("left") or {}, extracts), self._pick(spec.get("right") or {}, extracts)
        if lx is None or rx is None:
            return {"name": name, "skipped": "left or right extract not found"}
        try:
            lk, rk = self._col(lx, spec["left"]["key"]), self._col(rx, spec["right"]["key"])
            pairs = [(self._col(lx, a), self._col(rx, b)) for a, b in spec["fields"]]
        except KeyError as exc:
            return {"name": name, "skipped": str(exc)}
        compared = breaks = unmatched = 0
        for key, ll, lrow, rl, rrow in self.services.staging.paired_records(
                (lx.artefact.artefact_id, lx.idx(lk)), (rx.artefact.artefact_id, rx.idx(rk))):
            if rrow is None:
                unmatched += 1
                continue
            for a, b in pairs:
                compared += 1
                va, vb = (lx.value(lrow, a) or "").strip(), (rx.value(rrow, b) or "").strip()
                na, nb = _dec(va), _dec(vb)
                if na is not None and nb is not None:
                    tol = tolerance_for(cfg, rx.entity or "", b, "decimal")
                    if na == nb or tol.within(na, nb - na):
                        continue
                    delta = nb - na
                elif va == vb:
                    continue
                else:
                    delta = None
                breaks += 1
                self._emit(self._break(name, key, f"{a}~{b}", "cross_target_drift", va, va, vb, delta,
                                       [lx.evidence(ll, lrow, a), rx.evidence(rl, rrow, b)],
                                       note=f"{lx.artefact.path} vs {rx.artefact.path}"))
        return {"name": name, "compared": compared, "breaks": breaks, "unmatched": unmatched}

    # ------------------------------------------------------------------ control totals
    def _side(self, side: dict, extracts: list[Extract]) -> tuple[dict[str, Decimal], Extract | None, tuple | None]:
        ex = self._pick(side, extracts)
        if ex is None:
            raise KeyError(f"extract not found for {side}")
        value_col = self._col(ex, side["sum"])
        group_col = self._col(ex, side["group_by"]) if side.get("group_by") else None
        where = side.get("where")
        if where and not fields_of(where) <= {h.upper() for h in ex.header}:
            raise KeyError(f"condition fields not in {ex.artefact.path}: {describe(where)}")
        totals: dict[str, Decimal] = defaultdict(Decimal)
        sample = None
        for line, row in ex.rows():
            if where and not matches(where, row_getter(ex.header, row)):
                continue
            v = _dec(ex.value(row, value_col))
            if v is None:
                continue
            totals[(ex.value(row, group_col) or "").strip() if group_col else "*"] += v
            if sample is None:
                sample = (line, row, value_col)
        return dict(totals), ex, sample

    def _control(self, spec: dict, extracts: list[Extract]) -> dict:
        name, regime = spec.get("name") or "control", spec.get("regime")
        try:
            left, lx, lsample = self._side(spec["left"], extracts)
            if "value" in spec.get("right", {}):
                right, rx, rsample = {"*": Decimal(str(spec["right"]["value"]))}, None, None
            else:
                right, rx, rsample = self._side(spec["right"], extracts)
        except KeyError as exc:
            return {"name": name, "regime": regime, "skipped": str(exc), "reconciled": None}
        tol = Tolerance(Decimal(str((spec.get("tolerance") or {}).get("absolute_floor", "0"))),
                        Decimal(str((spec.get("tolerance") or {}).get("relative_bp", "0"))))
        mismatches = []
        for group in sorted(set(left) | set(right)):
            lv, rv = left.get(group, Decimal(0)), right.get(group, Decimal(0))
            if lv == rv or tol.within(lv, rv - lv):
                continue
            mismatches.append({"group": group, "left": format(lv, "f"), "right": format(rv, "f"),
                               "difference": format(rv - lv, "f")})
            evidence = []
            if lsample:
                evidence.append(lx.evidence(lsample[0], lsample[1], lsample[2]))
            if rsample and rx is not None:
                evidence.append(rx.evidence(rsample[0], rsample[1], rsample[2]))
            self._emit(self._break(name, group, spec["left"]["sum"], "control_mismatch", format(lv, "f"), format(lv, "f"),
                                   format(rv, "f"), rv - lv, evidence,
                                   tolerance=None if tol.exact else tol.as_dict(),
                                   note=f"{regime + ': ' if regime else ''}{name} - left total {format(lv, 'f')}, "
                                        f"right total {format(rv, 'f')}"))
        return {"name": name, "regime": regime, "groups": len(set(left) | set(right)),
                "left_total": format(sum(left.values(), Decimal(0)), "f"),
                "right_total": format(sum(right.values(), Decimal(0)), "f"),
                "reconciled": not mismatches, "mismatches": mismatches[:50]}
