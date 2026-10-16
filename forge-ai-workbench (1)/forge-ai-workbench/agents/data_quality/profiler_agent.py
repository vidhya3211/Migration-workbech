"""Profiler agent (T3): statistical profile of every column in the source extracts. Mechanical only.

Counts, nulls, distinct values, min/max, top values and type-pattern violations are computed by PostgreSQL
over the staged rows (no extract is loaded into memory); the inferred type comes from a bounded sample.
PII-classified columns are profiled for counts and pattern violations only - no values leave the store.
"""
from __future__ import annotations

from agents.base import AgentOutput, BaseAgent
from agents.data_quality.extracts import load_extracts
from blackboard.identity import agent_identity, register_agent
from ingestion.pii import column_matches
from reasoning.deterministic.transform import infer_type
from schemas.common import ReasoningMode, Tier
from schemas.dq import ColumnProfile
from schemas.mapping import DictionaryEntry
from schemas.signals import SignalType
from schemas.signals import SignalType as S


@register_agent
class ProfilerAgent(BaseAgent):
    IDENTITY = agent_identity("profiler_agent", Tier.T3, "data_quality", "Statistical profile of every column",
                              {S.COLUMN_PROFILES}, tools={"evidence.read"})

    def run(self, dictionary: list[DictionaryEntry], role: str = "source", **_: object) -> AgentOutput:
        settings = self.services.settings
        pii = settings.ingestion.pii
        profiles: list[ColumnProfile] = []
        for ex in load_extracts(self.services, self.run_id, role, dictionary):
            for col in ex.header:
                stats = ex.stats(col)
                is_pii = column_matches(col, pii.classification_columns + pii.obfuscate_columns)
                sample = [v for v in ex.sample(col, settings.scale.profile_sample_rows) if v and v.strip()]
                expected = ex.fields.get(col).data_type if col in ex.fields else None
                check = None if expected in (None, "string") else ("decimal" if expected == "decimal" else expected)
                violations = self.services.staging.violations(ex.artefact.artefact_id, ex.idx(col), check)
                profiles.append(ColumnProfile(
                    entity=ex.entity or ex.artefact.path, column=col, system=ex.artefact.system,
                    row_count=stats["rows"], null_count=stats["nulls"], distinct_count=stats["distinct"],
                    inferred_type=infer_type(sample), min_value=None if is_pii else stats["min"],
                    max_value=None if is_pii else stats["max"], top_values=[] if is_pii else stats["top"], pii=is_pii,
                    pattern_violations=violations))
                self.ledger.record_tool_call()
        self.services.kc.save_profiles(self.run_id, profiles)
        self.note_mode(ReasoningMode.DETERMINISTIC)
        self.signal(SignalType.COLUMN_PROFILES, f"profiled {len(profiles)} column(s)",
                    data={"columns": len(profiles), "pii_columns": sum(p.pii for p in profiles),
                          "rows": sum({(p.system.system_role, p.entity): p.row_count for p in profiles}.values()),
                          "type_violations": sum(p.pattern_violations for p in profiles)})
        return AgentOutput(items={"profiles": profiles})
