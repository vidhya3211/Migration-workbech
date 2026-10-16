"""Provenance and audit guardrails - applied before and after every knowledge-core write."""
from __future__ import annotations

from dataclasses import dataclass

from guardrails.deterministic.evidence import EvidenceVerifier
from guardrails.pipeline import GuardrailContext, Stage, block, ok
from schemas.execution import GuardrailResult
from schemas.provenance import Evidence


@dataclass
class EvidencedItem:
    item_id: str
    evidence: list[Evidence]


class ProvenancePolicy:
    """No certification without evidence: every item must carry at least one exact, re-verified locator."""

    name = "provenance_required"
    stages = frozenset({Stage.PROVENANCE})

    def __init__(self, verifier: EvidenceVerifier) -> None:
        self.verifier = verifier

    def check(self, subject: EvidencedItem, ctx: GuardrailContext, stage: Stage) -> GuardrailResult:
        if not subject.evidence:
            return block(stage, self.name, f"{subject.item_id}: missing provenance", subject.item_id)
        for ev in subject.evidence:
            reason = self.verifier.verify(ev)
            if reason:
                return block(stage, self.name, f"{subject.item_id}: {reason}", subject.item_id)
        return ok(stage, self.name, f"{subject.item_id}: {len(subject.evidence)} locator(s) verified", subject.item_id)


@dataclass
class AuditSubject:
    completeness: dict[str, dict[str, int]]


class AuditCompletenessPolicy:
    """Every governed write must have provenance + audit rows. Missing audit fails the run's assurance."""

    name = "audit_completeness"
    stages = frozenset({Stage.AUDIT})

    def check(self, subject: AuditSubject, ctx: GuardrailContext, stage: Stage) -> GuardrailResult:
        gaps = [f"{et}: {c['rows'] - c['with_audit']} without audit, {c['rows'] - c['with_provenance']} without provenance"
                for et, c in subject.completeness.items()
                if c["rows"] and (c["with_audit"] < c["rows"] or c["with_provenance"] < c["rows"])]
        if gaps:
            return block(stage, self.name, "; ".join(gaps))
        total = sum(c["rows"] for c in subject.completeness.values())
        return ok(stage, self.name, f"{total} governed row(s) fully audited")
