"""Deterministic guard: verifies every model / agent output against the real source before it can reach
the knowledge core (build doc §8: "keep it strict").
"""
from __future__ import annotations

from dataclasses import dataclass, field

from blackboard.control_plane import ControlPlaneViolation, get_identity, validate_agent_status
from guardrails.deterministic.evidence import EvidenceVerifier
from observability.logging.logger import Event, log_event
from schemas.common import ItemStatus, ReasoningMode, Tier
from schemas.provenance import CodeLocator, DocumentLocator, SchemaLocator
from schemas.rules import CandidateRule


@dataclass
class GuardOutcome:
    accepted: list = field(default_factory=list)
    rejected: list[tuple[object, str]] = field(default_factory=list)


class DeterministicGuard:
    def __init__(self, verifier: EvidenceVerifier) -> None:
        self.verifier = verifier

    # ------------------------------------------------------------------ rules
    def check_rule(self, rule: CandidateRule) -> str | None:
        art = self.verifier.artefacts.get(rule.artefact_id)
        if art is None:
            return "artefact not part of this run"
        if rule.evidence.artefact_id != rule.artefact_id:
            return "evidence cites a different artefact than the rule"
        if rule.run_id != art.run_id:
            return "rule run does not match artefact run"
        if rule.system != art.system:
            return "rule system tag does not match the upload it came from"
        reason = self.verifier.verify(rule.evidence)
        if reason:
            return reason
        if isinstance(rule.evidence.locator, (CodeLocator, DocumentLocator, SchemaLocator)):
            if not rule.anchor or rule.anchor.upper() not in rule.evidence.excerpt.upper():
                return f"anchor {rule.anchor!r} not present in the cited evidence"
        if not rule.name.strip() or not rule.expression.strip():
            return "rule has no name or no expression"
        try:
            identity = get_identity(rule.agent)
            if rule.tier != identity.tier:
                return f"agent {rule.agent} is {identity.tier} but emitted a {rule.tier} rule"
            validate_agent_status(rule.agent, rule.status)
        except ControlPlaneViolation as exc:
            return str(exc)
        if rule.status is not ItemStatus.CANDIDATE:
            return f"agents may only emit candidates, got {rule.status}"
        if identity.tier in (Tier.T1, Tier.T2) and not rule.requires_human:
            return f"{identity.tier} output must be routed to a human"
        if rule.reasoning_mode is ReasoningMode.LLM and not (rule.model and rule.prompt_version):
            return "LLM-derived rule without model/prompt_version cannot be audited"
        return None

    def verify_rules(self, rules: list[CandidateRule]) -> GuardOutcome:
        out = GuardOutcome()
        seen: set[str] = set()
        for rule in rules:
            reason = self.check_rule(rule)
            if reason is None and rule.rule_id in seen:
                reason = "duplicate rule id in this run"
            if reason:
                out.rejected.append((rule, reason))
                log_event(Event.DETERMINISTIC_GUARD_FAILED, reason, entity_id=rule.rule_id, agent=rule.agent)
            else:
                seen.add(rule.rule_id)
                out.accepted.append(rule)
        log_event(Event.DETERMINISTIC_GUARD_PASSED, f"guard accepted {len(out.accepted)} / refused {len(out.rejected)}",
                  accepted=len(out.accepted), refused=len(out.rejected))
        return out

    # ------------------------------------------------------------------ any evidenced item
    def verify_items(self, items: list, *, evidence_of=lambda x: x.evidence, id_of=lambda x: str(x)) -> GuardOutcome:
        """Generic check for differentials, mappings, cohorts, breaks, cases: all evidence must re-verify."""
        out = GuardOutcome()
        flat = []
        for item in items:
            ev = evidence_of(item)
            flat.extend(ev if isinstance(ev, list) else [ev])
        self.verifier.prefetch([e for e in flat if e is not None])
        for item in items:
            evidence = evidence_of(item)
            evidence = evidence if isinstance(evidence, list) else [evidence]
            if not evidence:
                reason = "no evidence"
            else:
                reason = next((r for ev in evidence if (r := self.verifier.verify(ev))), None)
            if reason:
                out.rejected.append((item, reason))
                log_event(Event.DETERMINISTIC_GUARD_FAILED, reason, entity_id=id_of(item))
            else:
                out.accepted.append(item)
        return out


def self_test(guard_factory) -> list[tuple[str, bool]]:
    """Proves the guard refuses fabricated evidence before a run starts (§40)."""
    from ingestion.evidence_store import EvidenceReader, MemoryEvidenceStore
    from schemas.artefact import Artefact
    from schemas.common import SystemRef, SystemRole, sha256_text
    from schemas.provenance import Evidence

    text = "       MOVE 1 TO WS-X.\n       MOVE 2 TO WS-Y."
    sys_ref = SystemRef(system_name="SELFTEST", system_role=SystemRole.SOURCE)
    sha = sha256_text(text)
    art = Artefact(artefact_id="A-SELFTEST", run_id="SELFTEST", upload_id="U", system=sys_ref, path="selftest.cbl",
                   artefact_type="cobol_program", sha256=sha, size_bytes=len(text), confidence=1.0, blob_uri="mem://x")
    guard: DeterministicGuard = guard_factory(EvidenceReader(MemoryEvidenceStore({"mem://x": text.encode()})),
                                               {art.artefact_id: art})
    good = CandidateRule(
        rule_id="R-SELFTEST", run_id="SELFTEST", artefact_id=art.artefact_id, agent="cobol_agent", tier=Tier.T1,
        kind="assignment", name="WS-X", canonical_name="X", expression="1", anchor="WS-X", confidence=0.9,
        system=sys_ref, evidence=Evidence(artefact_id=art.artefact_id, source_sha256=sha, system=sys_ref,
                                          excerpt="       MOVE 1 TO WS-X.",
                                          locator=CodeLocator(file="selftest.cbl", line_start=1, line_end=1)),
    )

    def ev(**kw):
        return good.evidence.model_copy(update=kw)

    cases = [
        ("accepts a rule whose evidence matches the source", good, True),
        ("refuses fabricated evidence", good.model_copy(update={"evidence": ev(excerpt="       MOVE 9 TO WS-X.")}), False),
        ("refuses a locator outside the file",
         good.model_copy(update={"evidence": ev(locator=CodeLocator(file="selftest.cbl", line_start=5, line_end=6))}), False),
        ("refuses an anchor absent from the evidence", good.model_copy(update={"anchor": "WS-Z"}), False),
        ("refuses agent self-certification", good.model_copy(update={"status": ItemStatus.CERTIFIED}), False),
        ("refuses a tier mismatch", good.model_copy(update={"tier": Tier.T3}), False),
        ("refuses an unregistered agent", good.model_copy(update={"agent": "rogue_agent"}), False),
        ("refuses a stale artefact version", good.model_copy(update={"evidence": ev(source_sha256="0" * 64)}), False),
        ("refuses an unaudited LLM rule", good.model_copy(update={"reasoning_mode": ReasoningMode.LLM}), False),
    ]
    return [(label, (guard.check_rule(rule) is None) == should_pass) for label, rule, should_pass in cases]
