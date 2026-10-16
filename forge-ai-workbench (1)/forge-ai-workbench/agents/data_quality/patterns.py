"""Population-level data-quality patterns (used by the Anomaly agent).

Per-column checks find records that break a certified rule. Migrations are also hurt by *patterns* no single
record reveals - the legacy history of the book. Each detector here is a SQL aggregate over the staged extract
(no extract is loaded into memory) and yields a cohort for the data steward:

* ``default_value_spike`` - one date value written for an implausible share of records (a conversion default),
  with its features: a contiguous block of keys, and records still recorded live with no recent activity;
* ``mixed_null_conventions`` - one column writing "no value" in more than one way (blank and a sentinel);
* ``orphan_reference`` - child records whose reference matches no parent record (declared relationships);
* ``era_correlated_nulls`` - a field empty far more often before some year than after it: a capture gap tied
  to the joining era, not to the person;
* ``flagged_population`` - project-defined populations that need treatment before cutover (claims in flight,
  gone-away customers, dormant accounts ...), each a configured condition.

Thresholds and conditions are project configuration (``heuristics.data_quality``), never constants.
"""
from __future__ import annotations

from typing import Iterator

from agents.data_quality.extracts import Extract
from reasoning.deterministic.predicates import describe, fields_of, matches, row_getter
from reasoning.heuristics.anomaly.scoring import score as anomaly_score
from schemas.common import stable_id
from schemas.dq import Cohort

DEFAULTS = {"spike_share": 0.01, "spike_min_records": 20, "spike_min_distinct": 10,
            "era_min_rate": 0.15, "era_ratio": 4.0, "era_min_support": 0.05, "era_min_records": 30,
            "relationships": [], "flag_cohorts": [], "stale_live": None}


class PatternDetector:
    def __init__(self, run_id: str, cfg: dict, sentinels: list[str], sample: int, evidence_n: int) -> None:
        self.run_id = run_id
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.sentinels = sentinels
        self.sample = sample
        self.evidence_n = evidence_n

    def detect(self, extracts: list[Extract], is_date) -> Iterator[Cohort]:
        by_entity = {x.entity.upper(): x for x in extracts if x.entity}
        for ex in extracts:
            if ex.entity is None:
                continue
            dates = [c for c in ex.header if is_date(c, ex.fields.get(c))]
            for col in dates:
                yield from self._spike(ex, col)
                yield from self._null_conventions(ex, col)
            era = self._era_column(ex, dates)
            if era:
                for col in ex.header:
                    if col != era and col != ex.key:
                        yield from self._era_nulls(ex, col, era)
            yield from self._flags(ex)
        yield from self._orphans(by_entity)

    # ------------------------------------------------------------------ helpers
    def _cohort(self, ex: Extract, col: str | None, code: str, description: str, n: int, lines: list[int],
                facts: list[str], tag: str = "") -> Cohort:
        rows = ex.rows_at(lines[:self.sample]) if lines else {}
        h = anomaly_score(f"{ex.entity}.{col}:{code}", code, n, ex.row_count, backed_by_certified_rule=False)
        h = h.model_copy(update={"evidence": h.evidence + facts})
        return Cohort(cohort_id=stable_id("COH", self.run_id, ex.entity, col or "-", code, tag), run_id=self.run_id,
                      entity=ex.entity, column=col, defect_code=code, description=description, record_count=n,
                      record_refs=[ex.record_key(r) for r in rows.values()],
                      anomaly=h, evidence=[ex.evidence(ln, r, col) for ln, r in list(rows.items())[:self.evidence_n]])

    # ------------------------------------------------------------------ detectors
    def _spike(self, ex: Extract, col: str) -> Iterator[Cohort]:
        stats = ex.stats(col, top=1)
        if not stats["top"] or stats["distinct"] < self.cfg["spike_min_distinct"]:
            return     # a column with few distinct dates (e.g. a valuation date) is not a spike candidate
        value, n = stats["top"][0]
        if value in self.sentinels or n < self.cfg["spike_min_records"] or n / max(1, stats["rows"]) <= self.cfg["spike_share"]:
            return
        facts = [f"{n} of {stats['rows']} records ({n / stats['rows']:.1%}) hold {col} = {value}"]
        if ex.key:
            span = ex.staging.value_key_span(ex.artefact.artefact_id, ex.idx(col), value, ex.idx(ex.key))
            if span["span"] is not None and span["numeric_keys"] > 2 and span["span"] < span["numeric_keys"] * 3:
                facts.append(f"their keys form a contiguous block (span {span['span']} for {span['numeric_keys']} keys)"
                             " - written by one process")
        lines = ex.staging.lines_with_value(ex.artefact.artefact_id, ex.idx(col), value, self.sample)
        stale = self.cfg.get("stale_live")
        if stale and lines:
            rows = ex.rows_at(lines)
            get = lambda r: row_getter(ex.header, r)   # noqa: E731
            live = [r for r in rows.values() if matches(stale["live_when"], get(r))]
            quiet = [r for r in live if matches(stale["inactive_when"], get(r))]
            if live and len(quiet) > 0.5 * len(live):
                facts.append(f"{len(quiet)} of {len(live)} sampled live records show no recent activity "
                             f"({describe(stale['inactive_when'])}) - recorded live, behaving archived")
        yield self._cohort(ex, col, "default_value_spike",
                           f"{col}: {value} written for {n} records - a conversion default, not a real date",
                           n, lines, facts, value)

    def _null_conventions(self, ex: Extract, col: str) -> Iterator[Cohort]:
        c = ex.staging.null_conventions(ex.artefact.artefact_id, ex.idx(col), self.sentinels)
        if not (c["blank"] and c["sentinel"]):
            return
        n = c["blank"] + c["sentinel"]
        lines = ex.staging.lines_where_null_like(ex.artefact.artefact_id, ex.idx(col), self.sentinels, self.sample)
        yield self._cohort(ex, col, "mixed_null_conventions",
                           f"{col} writes 'no value' two ways: {c['blank']} blank and {c['sentinel']} sentinel "
                           f"(alongside {c['real']} real values) - one convention must be chosen before mapping",
                           n, lines, [f"conventions: blank {c['blank']}, sentinel {c['sentinel']}, real {c['real']}"])

    def _era_column(self, ex: Extract, dates: list[str]) -> str | None:
        configured = (self.cfg.get("era_columns") or {}).get(ex.entity)
        if configured:
            return next((h for h in ex.header if h.upper() == str(configured).upper()), None)
        for col in dates:   # the first date column with a real spread of years
            if len(ex.staging.null_rate_by_year(ex.artefact.artefact_id, ex.idx(col), ex.idx(col))) >= 5:
                return col
        return None

    def _era_nulls(self, ex: Extract, col: str, era: str) -> Iterator[Cohort]:
        years = ex.staging.null_rate_by_year(ex.artefact.artefact_id, ex.idx(col), ex.idx(era))
        total = sum(n for _, n, _ in years)
        empty = sum(e for _, _, e in years)
        if total == 0 or empty == 0 or empty == total:
            return
        # a pattern is a population statement: both sides of the cut need volume, not just a share
        min_side = max(self.cfg["era_min_support"] * total, float(self.cfg["era_min_records"]))
        best = None
        before_n = before_e = 0
        for i, (y, n, e) in enumerate(years[:-1]):
            before_n, before_e = before_n + n, before_e + e
            after_n, after_e = total - before_n, empty - before_e
            if before_n < min_side or after_n < min_side:
                continue
            r_pre, r_post = before_e / before_n, after_e / after_n
            if r_pre > self.cfg["era_min_rate"] and r_pre > self.cfg["era_ratio"] * max(r_post, 0.01):
                gap = r_pre - r_post
                if best is None or gap > best[0]:
                    best = (gap, years[i + 1][0], r_pre, r_post)
        if best is None:
            return
        _, cut, r_pre, r_post = best
        n, lines = ex.staging.lines_null_before_year(ex.artefact.artefact_id, ex.idx(col), ex.idx(era), cut, self.sample)
        yield self._cohort(ex, col, "era_correlated_nulls",
                           f"{col} is empty for {r_pre:.0%} of records with {era} before {cut}, against {r_post:.0%} "
                           "after - a capture gap tied to the era, not to the customer", n, lines,
                           [f"null rate before {cut}: {r_pre:.3f}; from {cut}: {r_post:.3f}"], str(cut))

    def _orphans(self, by_entity: dict[str, Extract]) -> Iterator[Cohort]:
        for rel in self.cfg["relationships"]:
            ce, _, cc = str(rel["child"]).upper().rpartition(".")
            pe, _, pc = str(rel["parent"]).upper().rpartition(".")
            child, parent = by_entity.get(ce), by_entity.get(pe)
            if child is None or parent is None:
                continue
            ccol = next((h for h in child.header if h.upper() == cc), None)
            pcol = next((h for h in parent.header if h.upper() == pc), None)
            if ccol is None or pcol is None:
                continue
            n, lines = child.staging.orphans(child.artefact.artefact_id, child.idx(ccol), parent.artefact.artefact_id,
                                             parent.idx(pcol), self.sample)
            if n:
                yield self._cohort(child, ccol, "orphan_reference",
                                   f"{n} {child.entity} record(s) refer through {ccol} to a {parent.entity} that does "
                                   "not exist", n, lines, [f"relationship {rel['child']} -> {rel['parent']}"],
                                   rel["parent"])

    def _flags(self, ex: Extract) -> Iterator[Cohort]:
        specs = [f for f in self.cfg["flag_cohorts"]
                 if fields_of(f["when"]) <= {h.upper() for h in ex.header}
                 and (not f.get("entity") or str(f["entity"]).upper() == ex.entity.upper())]
        if not specs:
            return
        hits = {f["name"]: (0, []) for f in specs}
        for line_no, row in ex.rows():
            get = row_getter(ex.header, row)
            for f in specs:
                if matches(f["when"], get):
                    n, lines = hits[f["name"]]
                    if len(lines) < self.sample:
                        lines.append(line_no)
                    hits[f["name"]] = (n + 1, lines)
        for f in specs:
            n, lines = hits[f["name"]]
            if n:
                col = next((h for h in ex.header if h.upper() in fields_of(f["when"])), None)
                yield self._cohort(ex, col, "flagged_population",
                                   f"{f['name']}: {f.get('description') or describe(f['when'])}", n, lines,
                                   [f"condition: {describe(f['when'])}"], f["name"])
