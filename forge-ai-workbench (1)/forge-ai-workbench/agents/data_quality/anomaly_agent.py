"""Anomaly agent (T2): groups records sharing a defect into a cohort, judged against certified rules/schema.

"Correct" comes from the certified knowledge core (88-level domains, CHECK constraints, NOT NULL, ranges,
primary keys) - not from the data's own majority. Cohorts are proposals for the data steward.

One streaming pass over the staged rows per extract (bounded memory: a cohort keeps its count plus a capped
sample of record references and evidence); duplicate keys are found by PostgreSQL ``GROUP BY``.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from agents.base import AgentOutput, BaseAgent
from agents.data_quality.extracts import Extract, load_extracts
from agents.data_quality.patterns import PatternDetector
from blackboard.identity import agent_identity, register_agent
from reasoning.deterministic.naming import canonical_name
from reasoning.deterministic.transform import TransformError, parse_date
from reasoning.heuristics.anomaly.scoring import score as anomaly_score
from schemas.common import ReasoningMode, Tier, stable_id
from schemas.dq import Cohort
from schemas.mapping import DictionaryEntry
from schemas.signals import SignalType
from schemas.signals import SignalType as S

DEFAULT_SENTINELS = ["00000000", "99999999", "0000-00-00", "9999-12-31", "19000101", "1900-01-01"]
DESCRIPTIONS = {"null_mandatory": "{col} is empty but the schema requires it",
                "invalid_code": "{col} holds code(s) outside the certified domain",
                "sentinel_value": "{col} holds a legacy sentinel instead of a real date",
                "invalid_date": "{col} is not a valid date",
                "out_of_range": "{col} is outside the allowed range",
                "duplicate_key": "{col} key appears more than once"}


def _is_date_column(col: str, entry: DictionaryEntry | None) -> bool:
    if entry and entry.data_type == "date":
        return True
    desc = (entry.description or "") if entry else ""
    return bool(re.search(r"YYYYMMDD|date", desc, re.I)) or bool(re.search(r"(_DT|_DATE|DOB)$", col, re.I))


class _Hits:
    """Count + capped sample per (column, defect)."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.count: dict[tuple[str, str], int] = defaultdict(int)
        self.sample: dict[tuple[str, str], list[tuple[int, list[str]]]] = defaultdict(list)
        self.facts: dict[tuple[str, str], set] = defaultdict(set)

    def add(self, key: tuple[str, str], line_no: int, row: list[str], fact: str | None = None) -> None:
        self.count[key] += 1
        if len(self.sample[key]) < self.cap:
            self.sample[key].append((line_no, row))
        if fact is not None and len(self.facts[key]) < 20:
            self.facts[key].add(fact)


@register_agent
class AnomalyAgent(BaseAgent):
    IDENTITY = agent_identity("anomaly_agent", Tier.T2, "data_quality", "Groups records sharing a defect into a cohort",
                              {S.DQ_COHORTS}, tools={"evidence.read"}, proposes={"cohort"})

    def run(self, dictionary: list[DictionaryEntry], certified_domains: dict[str, dict] | None = None,
            **_: object) -> AgentOutput:
        """``certified_domains``: canonical column -> {"values": [...], "rule_ids": [...]} from certified rules."""
        certified_domains = certified_domains or {}
        cohorts: list[Cohort] = []
        extracts = load_extracts(self.services, self.run_id, "source", dictionary)
        for ex in extracts:
            if ex.entity is None:
                continue
            cohorts += self._scan(ex, certified_domains)
        # population patterns no single record shows (conversion defaults, mixed conventions, orphans, eras, flags)
        h = self.services.settings.heuristics
        scale = self.services.settings.scale
        sentinels = list(h.get("anomaly", {}).get("date_sentinels", DEFAULT_SENTINELS))
        detector = PatternDetector(self.run_id, h.get("data_quality") or {}, sentinels, scale.cohort_record_refs,
                                   scale.cohort_evidence)
        cohorts += list(detector.detect(extracts, _is_date_column))
        self.note_mode(ReasoningMode.DETERMINISTIC)
        self.note_mode(ReasoningMode.HEURISTIC)
        self.signal(SignalType.DQ_COHORTS, f"{len(cohorts)} defect cohort(s) across source extracts",
                    item_refs=[c.cohort_id for c in cohorts],
                    data={"by_defect": _count(c.defect_code for c in cohorts),
                          "records_affected": sum(c.record_count for c in cohorts)})
        return AgentOutput(items={"cohorts": cohorts})

    def _scan(self, ex: Extract, certified: dict[str, dict]) -> list[Cohort]:
        scale = self.services.settings.scale
        sentinels = set(self.services.settings.heuristics.get("anomaly", {}).get("date_sentinels", DEFAULT_SENTINELS))
        hits = _Hits(scale.cohort_record_refs)
        checks = []
        for col in ex.header:
            entry = ex.fields.get(col)
            if entry is None:
                continue
            domain = set(certified.get(canonical_name(col), {}).get("values") or entry.allowed_values or [])
            rng = None
            if entry.range:
                try:
                    rng = (float(entry.range[0]), float(entry.range[1]))
                except ValueError:
                    rng = None
            checks.append((ex.idx(col), col, entry, domain, _is_date_column(col, entry), rng))
        for line_no, row in ex.rows():
            for i, col, entry, domain, is_date, rng in checks:
                v = (row[i] if i < len(row) else "").strip()
                if v == "":
                    if not entry.nullable:
                        hits.add((col, "null_mandatory"), line_no, row)
                    continue
                if domain and v not in domain:
                    hits.add((col, "invalid_code"), line_no, row, v)
                if is_date:
                    if v in sentinels:
                        hits.add((col, "sentinel_value"), line_no, row, v)
                    else:
                        try:
                            parse_date(v)
                        except TransformError:
                            hits.add((col, "invalid_date"), line_no, row, v)
                if rng is not None:
                    try:
                        if not rng[0] <= float(v) <= rng[1]:
                            hits.add((col, "out_of_range"), line_no, row, f"{entry.range[0]}..{entry.range[1]}")
                    except ValueError:
                        pass
        if ex.key:
            dup_lines = [ln for _, lines in ex.duplicates(ex.key) for ln in lines]
            rows = ex.rows_at(dup_lines[:scale.cohort_record_refs])
            for ln in dup_lines:
                hits.count[(ex.key, "duplicate_key")] += 1
                if ln in rows and len(hits.sample[(ex.key, "duplicate_key")]) < hits.cap:
                    hits.sample[(ex.key, "duplicate_key")].append((ln, rows[ln]))
        out = []
        for (col, defect), n in sorted(hits.count.items()):
            entry = ex.fields.get(col)
            rule_ids = certified.get(canonical_name(col), {}).get("rule_ids", []) if defect == "invalid_code" else []
            h = anomaly_score(f"{ex.entity}.{col}:{defect}", defect, n, ex.row_count,
                              backed_by_certified_rule=bool(rule_ids) or entry is not None)
            sample = hits.sample[(col, defect)]
            evidence = [ex.evidence(ln, row, col) for ln, row in sample[:scale.cohort_evidence]]
            if entry is not None:
                evidence.append(entry.evidence)
            facts = hits.facts[(col, defect)]
            out.append(Cohort(
                cohort_id=stable_id("COH", self.run_id, ex.entity, col, defect), run_id=self.run_id, entity=ex.entity,
                column=col, defect_code=defect, description=DESCRIPTIONS[defect].format(col=col), record_count=n,
                record_refs=[ex.record_key(row) for _, row in sample],
                anomaly=h.model_copy(update={"evidence": h.evidence + [OBSERVED + json.dumps(sorted(facts)[:5])]
                                             if facts and defect != "null_mandatory" else h.evidence}),
                rule_ids=rule_ids, evidence=evidence))
        return out


OBSERVED = "observed: "

def _count(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out
