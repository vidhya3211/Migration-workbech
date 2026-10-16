"""Validation agent (T2): independently checks the Mapping agent's work before the steward sees it.

Different evidence from the Mapping agent: schema facts, a dry run of every transformation over the real
source values, nullability against observed nulls, code-domain coverage and one-to-one integrity.

The dry run applies each transformation once per *distinct* source value (counted by PostgreSQL) and weighs
the result by how many records carry that value - every record is covered without visiting every row. Above
``scale.validation_distinct_cap`` distinct values the most frequent are tested and the finding says so.
"""
from __future__ import annotations

from collections import Counter

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from agents.data_quality.extracts import load_extracts
from reasoning.deterministic.schema import compare as schema_compare
from reasoning.deterministic.transform import TransformError, apply
from schemas.common import Tier, ReasoningMode
from schemas.mapping import DictionaryEntry, MappingProposal, MappingValidation, ValidationFinding
from schemas.signals import SignalType


@register_agent
class ValidationAgent(BaseAgent):
    IDENTITY = agent_identity("validation_agent", Tier.T2, "mapping_build", "Independently checks the Mapping agent's work", {S.HUMAN_REVIEW_REQUIRED, S.MAPPING_VALIDATION})

    def run(self, mappings: list[MappingProposal], dictionary: list[DictionaryEntry], **_: object) -> AgentOutput:
        extracts = {e.entity: e for e in load_extracts(self.services, self.run_id, "source", dictionary) if e.entity}
        targets = Counter(f"{m.target.entity}.{m.target.field}" for m in mappings)
        out: list[MappingValidation] = []
        for m in mappings:
            f: list[ValidationFinding] = []
            for c in schema_compare(m.source, m.target):
                f.append(ValidationFinding(mapping_id=m.mapping_id, check=c.check, passed=c.passed, severity=c.severity,
                                           detail=c.detail))
            dup = targets[f"{m.target.entity}.{m.target.field}"]
            f.append(ValidationFinding(mapping_id=m.mapping_id, check="one_to_one", passed=dup == 1,
                                       severity="block" if dup > 1 else "info",
                                       detail=f"target attribute mapped {dup} time(s)"))
            ex = extracts.get(m.source.entity)
            if ex is not None and m.source.field in ex.header:
                distinct, truncated = ex.distinct(m.source.field, self.services.settings.scale.validation_distinct_cap)
                total = sum(k for _, k in distinct)
                errors, nulls, produced = 0, 0, set()
                for v, k in distinct:
                    try:
                        out_v = apply(m.transformation, v)
                        produced.add(out_v)
                        nulls += k if (out_v is None or out_v == "") else 0
                    except TransformError:
                        errors += k
                note = (f" (most frequent {len(distinct)} distinct values tested, covering {total} record(s))"
                        if truncated else "")
                f.append(ValidationFinding(mapping_id=m.mapping_id, check="transformation_dry_run", passed=errors == 0,
                                           severity="warn" if errors else "info",
                                           detail=f"{errors} of {total} source value(s) fail the transformation{note}"))
                if nulls and not m.target.nullable:
                    f.append(ValidationFinding(mapping_id=m.mapping_id, check="observed_nulls", passed=False,
                                               severity="warn",
                                               detail=f"{nulls} record(s) would load NULL into a NOT NULL target"))
                if m.target.allowed_values:
                    bad = sorted({p for p in produced if p is not None and p not in m.target.allowed_values})
                    f.append(ValidationFinding(mapping_id=m.mapping_id, check="domain_coverage", passed=not bad,
                                               severity="warn" if bad else "info",
                                               detail=f"values not allowed by the target: {bad[:10]}" if bad
                                               else "all transformed values allowed by the target"))
            blocks = any(not x.passed and x.severity == "block" for x in f)
            warns = any(not x.passed and x.severity == "warn" for x in f)
            out.append(MappingValidation(mapping_id=m.mapping_id,
                                         verdict="disagree" if blocks else "concerns" if warns else "agree", findings=f))
        self.note_mode(ReasoningMode.DETERMINISTIC)
        self.signal(SignalType.MAPPING_VALIDATION, f"validated {len(out)} mapping(s): "
                    f"{sum(v.verdict == 'agree' for v in out)} agree, {sum(v.verdict == 'concerns' for v in out)} "
                    f"with concerns, {sum(v.verdict == 'disagree' for v in out)} disagree",
                    item_refs=[v.mapping_id for v in out])
        return AgentOutput(items={"validations": out})
