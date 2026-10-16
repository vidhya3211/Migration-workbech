"""Reconciler agent (T3): compares loaded target values against source values through certified mappings.

Mechanical: join on the certified key mapping, apply each certified transformation to the source value, compare
by the target's data type - under the project's two-tier tolerance (absolute floor or relative basis points,
whichever is larger), so rounding is not reported as a break. Only certified mappings are used.

What a production reconciliation also needs (``heuristics.reconciliation``, all optional, per project):

* **date alignment** - where values are struck at an effective / valuation date, records match on key *and*
  date, so a value is never compared with one struck on another day (the most common false break);
* **migration scope** - records the programme decided not to migrate (conditions per source entity, with a
  reason) are accounted as *excluded*, not reported as missing customers;
* **load rejections** - a target load-rejection report turns "missing" into "rejected at load, because ...";
* **the record waterfall** - every source record lands in exactly one bucket: excluded, rejected, missing,
  or loaded; loaded records are reconciled or carry breaks. Nothing is unaccounted for.

Scale: the source's *transformed* keys are COPY'd into ``recon_key`` and joined to the target extract inside
PostgreSQL; the join result streams back through a server-side cursor and breaks leave in batches through
``sink`` (guard + bulk persistence in the graph node). Memory stays flat whatever the extract sizes.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from typing import Callable, Iterator

from agents.base import AgentOutput, BaseAgent
from agents.data_quality.extracts import Extract, load_extracts
from blackboard.identity import agent_identity, register_agent
from reasoning.deterministic.predicates import describe, matches, row_getter
from reasoning.deterministic.tolerance import tolerance_for
from reasoning.deterministic.transform import TransformError, apply, to_decimal, values_equal
from schemas.common import ReasoningMode, Tier, stable_id
from schemas.mapping import DictionaryEntry, MappingProposal
from schemas.recon import ReconciliationBreak
from schemas.signals import SignalType
from schemas.signals import SignalType as S

Sink = Callable[[list[ReconciliationBreak]], None]


@register_agent
class ReconcilerAgent(BaseAgent):
    IDENTITY = agent_identity("reconciler_agent", Tier.T3, "reconciliation", "Compares loaded source vs target values",
                              {S.RECON_BREAKS}, tools={"deterministic.expression", "evidence.read"},
                              proposes={"reconciliation_break"})

    def run(self, certified_mappings: list[MappingProposal], dictionary: list[DictionaryEntry],
            sink: Sink | None = None, **_: object) -> AgentOutput:
        cfg = self.services.settings.heuristics.get("reconciliation") or {}
        collected: list[ReconciliationBreak] = []
        batch_size = self.services.settings.scale.bulk_batch_size
        buf: list[ReconciliationBreak] = []
        by_type: dict[str, int] = defaultdict(int)

        def emit(b: ReconciliationBreak) -> None:
            by_type[b.break_type] += 1
            buf.append(b)
            if len(buf) >= batch_size:
                flush()

        def flush() -> None:
            if not buf:
                return
            if sink is None:
                collected.extend(buf)
            else:
                sink(list(buf))
            buf.clear()

        all_src = load_extracts(self.services, self.run_id, "source", dictionary)
        all_tgt = load_extracts(self.services, self.run_id, "target", dictionary)
        src_x = {e.entity: e for e in all_src if e.entity}
        tgt_x = {e.entity: e for e in all_tgt if e.entity}
        rejections = self._load_rejections(cfg.get("load_rejections") or [], all_src + all_tgt)
        groups: dict[tuple[str, str], list[MappingProposal]] = defaultdict(list)
        for m in certified_mappings:
            groups[(m.source.entity, m.target.entity)].append(m)
        summary: dict[str, dict] = {}
        waterfall: list[dict] = []
        staging = self.services.staging
        for (se, te), ms in sorted(groups.items()):
            key_map = next((m for m in ms if m.source.primary_key), None)
            sx, tx = src_x.get(se), tgt_x.get(te)
            if key_map is None or sx is None or tx is None:
                summary[f"{se}->{te}"] = {"skipped": "no certified key mapping or extract"}
                continue
            pair_id = stable_id("RP", self.run_id, se, te)
            s_key, t_key = key_map.source.field, key_map.target.field
            s_idx, t_idx = sx.idx(s_key), tx.idx(t_key)
            align = self._alignment(cfg, se, te, ms, sx, tx)
            scope = [c for c in (cfg.get("scope") or {}).get(se, [])]

            def source_key(row: list[str]) -> str:
                try:
                    k = apply(key_map.transformation, row[s_idx] if s_idx < len(row) else None)
                except TransformError:
                    k = None
                k = (k or "").strip()
                if align is not None:
                    k = f"{k}|{align['source_date'](row)}"
                return k

            def keys() -> Iterator[tuple[str, int]]:
                for line_no, row in sx.rows():
                    yield source_key(row), line_no

            loaded = staging.load_recon_keys(self.run_id, pair_id, keys())
            fields = [m for m in ms if m is not key_map and m.source.field in sx.header and m.target.field in tx.header
                      and (align is None or m.target.field != align["target_field"])]
            tols = {m.mapping_id: tolerance_for(cfg, te, m.target.field, m.target.data_type) for m in fields}
            matched = compared = with_breaks = 0
            excluded: Counter = Counter()
            excluded_but_loaded: Counter = Counter()
            rejected: Counter = Counter()
            missing = 0
            breaks_before = sum(by_type.values())
            pair_rejections = rejections.get(te, {}) or rejections.get("*", {})
            for skey, s_line, srow, t_line, trow in staging.recon_source_join(
                    self.run_id, pair_id, sx.artefact.artefact_id, tx.artefact.artefact_id, t_idx,
                    align["target_idx"] if align else None):
                reason = self._out_of_scope(scope, sx.header, srow)
                record_key, eff = (skey.rsplit("|", 1) if align is not None else (skey, None))
                if t_line is None:
                    if reason is not None:
                        excluded[reason] += 1
                        continue
                    shown_key = record_key or f"(no key, source line {s_line})"
                    rej = pair_rejections.get(record_key)
                    if rej is not None:
                        rejected[rej or "rejected"] += 1
                        emit(self._break(se, te, shown_key, t_key, "rejected_at_load", sx.value(srow, s_key),
                                         record_key or None, None, None, key_map, [sx.evidence(s_line, srow, s_key)],
                                         eff=eff, note=rej or "rejected by the target load"))
                        continue
                    missing += 1
                    emit(self._break(se, te, shown_key, t_key, "missing_in_target", sx.value(srow, s_key),
                                     record_key or None, None, None, key_map, [sx.evidence(s_line, srow, s_key)], eff=eff))
                    continue
                if reason is not None:
                    excluded_but_loaded[reason] += 1
                matched += 1
                broke = False
                for m in fields:
                    compared += 1
                    sv, tv = sx.value(srow, m.source.field), tx.value(trow, m.target.field)
                    try:
                        expected = apply(m.transformation, sv)
                    except TransformError:
                        broke = True
                        emit(self._break(se, te, record_key, m.target.field, "transform_error", sv, None, tv, None, m,
                                         [sx.evidence(s_line, srow, m.source.field),
                                          tx.evidence(t_line, trow, m.target.field)], eff=eff))
                        continue
                    equal, delta = values_equal(expected, tv, m.target.data_type)
                    tol = tols[m.mapping_id]
                    if not equal and delta is not None and not tol.exact:
                        base = to_decimal(expected)
                        equal = base is not None and tol.within(base, delta)
                    if not equal:
                        broke = True
                        emit(self._break(se, te, record_key, m.target.field, "value_mismatch", sv, expected, tv, delta,
                                         m, [sx.evidence(s_line, srow, m.source.field),
                                             tx.evidence(t_line, trow, m.target.field)],
                                         eff=eff, tolerance=None if tol.exact else tol.as_dict()))
                with_breaks += broke
            unexpected = 0
            for t_line, trow in staging.recon_unexpected(self.run_id, pair_id, tx.artefact.artefact_id, t_idx,
                                                         align["target_idx"] if align else None):
                unexpected += 1
                k = tx.value(trow, t_key) or f"(no key, target line {t_line})"
                emit(self._break(se, te, k, t_key, "unexpected_in_target", None, None, k, None, key_map,
                                 [tx.evidence(t_line, trow, t_key)],
                                 eff=tx.value(trow, align["target_field"]) if align else None))
            staging.drop_recon_keys(self.run_id, pair_id)
            wf = {"pair": f"{se}->{te}", "source_entity": se, "target_entity": te, "extracted": sx.row_count,
                  "excluded": sum(excluded.values()), "excluded_reasons": dict(excluded),
                  "in_scope": sx.row_count - sum(excluded.values()),
                  "rejected": sum(rejected.values()), "rejected_reasons": dict(rejected),
                  "missing": missing, "loaded": matched, "reconciled": matched - with_breaks,
                  "with_breaks": with_breaks, "unexpected_in_target": unexpected,
                  "excluded_but_loaded": dict(excluded_but_loaded),
                  "date_aligned": align["label"] if align else None}
            wf["balanced"] = wf["extracted"] == wf["excluded"] + wf["rejected"] + wf["missing"] + wf["loaded"]
            waterfall.append(wf)
            summary[f"{se}->{te}"] = {"source_records": sx.row_count, "target_records": tx.row_count,
                                      "keys_loaded": loaded, "matched": matched, "fields_compared": compared,
                                      "breaks": sum(by_type.values()) - breaks_before}
        flush()
        total = sum(by_type.values())
        self.note_mode(ReasoningMode.DETERMINISTIC)
        msg = f"{total} reconciliation break(s)"
        for wf in waterfall:
            msg += (f"; {wf['pair']}: extracted {wf['extracted']} = excluded {wf['excluded']} + rejected "
                    f"{wf['rejected']} + missing {wf['missing']} + loaded {wf['loaded']} "
                    f"(reconciled {wf['reconciled']})")
        self.signal(SignalType.RECON_BREAKS, msg, item_refs=[b.break_id for b in collected][:500],
                    data={"by_entity": summary, "by_type": dict(by_type), "total": total, "waterfall": waterfall})
        return AgentOutput(items={"breaks": collected, "recon_summary": {"by_entity": summary, "by_type": dict(by_type),
                                                                          "total": total, "waterfall": waterfall}})

    # ------------------------------------------------------------------ project policy
    @staticmethod
    def _out_of_scope(rules: list[dict], header: list[str], row: list[str]) -> str | None:
        if not rules:
            return None
        get = row_getter(header, row)
        for r in rules:
            if matches(r, get):
                return r.get("reason") or f"out of migration scope ({describe(r)})"
        return None

    @staticmethod
    def _load_rejections(specs: list[dict], extracts: list[Extract]) -> dict[str, dict[str, str]]:
        """Load-rejection reports: ``{target entity or *: {record key: reason}}``. Kept in memory - a rejection
        report is a small fraction of a load by construction."""
        out: dict[str, dict[str, str]] = {}
        for spec in specs:
            for ex in extracts:
                hit = (spec.get("entity") and ex.entity and ex.entity.upper() == str(spec["entity"]).upper()) or \
                      (spec.get("path_contains") and str(spec["path_contains"]).lower() in ex.artefact.path.lower())
                if not hit:
                    continue
                key_col = next((h for h in ex.header if h.upper() == str(spec["key_field"]).upper()), None)
                reason_col = next((h for h in ex.header if h.upper() == str(spec.get("reason_field", "")).upper()), None)
                if key_col is None:
                    continue
                bucket = out.setdefault(str(spec.get("target_entity") or "*"), {})
                for _, row in ex.rows():
                    k = (ex.value(row, key_col) or "").strip()
                    if k:
                        bucket[k] = (ex.value(row, reason_col) or "").strip() if reason_col else ""
        return out

    @staticmethod
    def _alignment(cfg: dict, se: str, te: str, ms: list[MappingProposal], sx: Extract, tx: Extract) -> dict | None:
        spec = (cfg.get("alignment") or {}).get(f"{se}->{te}") or (cfg.get("alignment") or {}).get(te)
        if not spec:
            return None
        s_field = next((h for h in sx.header if h.upper() == str(spec["source_date"]).upper()), None)
        t_field = next((h for h in tx.header if h.upper() == str(spec["target_date"]).upper()), None)
        if s_field is None or t_field is None:
            return None
        date_map = next((m for m in ms if m.source.field == s_field and m.target.field == t_field), None)
        s_idx = sx.idx(s_field)

        def source_date(row: list[str]) -> str:
            v = row[s_idx] if s_idx < len(row) else None
            if date_map is not None:
                try:
                    v = apply(date_map.transformation, v)
                except TransformError:
                    pass
            return (v or "").strip()
        return {"source_date": source_date, "target_idx": tx.idx(t_field), "target_field": t_field,
                "label": f"{s_field} = {t_field}"}

    def _break(self, se: str, te: str, key: str, field: str, kind: str, sv, expected, tv, delta: Decimal | None,
               m: MappingProposal, evidence, *, eff: str | None = None, tolerance: dict | None = None,
               note: str | None = None) -> ReconciliationBreak:
        return ReconciliationBreak(
            break_id=stable_id("BRK", self.run_id, te, key, field, kind, eff or ""), run_id=self.run_id,
            record_key=key, entity=te, field=field, break_type=kind, source_value=sv, expected_value=expected,
            target_value=tv, delta=None if delta is None else format(delta, "f"), mapping_id=m.mapping_id if m else None,
            tolerance=tolerance, effective_date=eff or None, note=note, evidence=evidence)
