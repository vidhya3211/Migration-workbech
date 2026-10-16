"""Central control plane (§24-25): tier enforcement as executable policy.

* T1 - evidence gathering / recommendation only; cannot certify.
* T2 - may propose a complete result; requires named human approval.
* T3 - deterministic / mechanical only; may run automatically.

No fallback path may increase autonomy, and a timeout, error or missing human response can only
move an item to ``unresolved`` - never to ``certified``.
"""
from __future__ import annotations

from blackboard.identity import AGENTS, AgentIdentity, get_identity
from schemas.common import HumanRole, ItemStatus, Tier
from schemas.signals import Signal


class ControlPlaneViolation(Exception):
    """A tier, signal, tool or write violation. Always explicit; never swallowed."""


AGENT_STATUSES: dict[Tier, frozenset[ItemStatus]] = {
    Tier.T1: frozenset({ItemStatus.CANDIDATE, ItemStatus.PROPOSED}),
    Tier.T2: frozenset({ItemStatus.CANDIDATE, ItemStatus.PROPOSED}),
    Tier.T3: frozenset({ItemStatus.CANDIDATE, ItemStatus.AUTOMATIC, ItemStatus.PROPOSED}),
}
HUMAN_ONLY = frozenset({ItemStatus.CERTIFIED, ItemStatus.REJECTED})

# item type -> the human role that may decide it (§41)
DECISION_ROLES: dict[str, HumanRole] = {
    "differential": HumanRole.ACTUARY_SME,
    "rule": HumanRole.ACTUARY_SME,
    "mapping": HumanRole.DATA_STEWARD,
    "entity_match": HumanRole.DATA_STEWARD,
    "cohort": HumanRole.DATA_STEWARD,
    "break_cluster": HumanRole.RECONCILIATION_ANALYST,
    "reconciliation_break": HumanRole.RECONCILIATION_ANALYST,
    "conduct_case": HumanRole.CONDUCT_OFFICER,
    "go_no_go": HumanRole.MIGRATION_LEAD,
}


ALLOWED_DECISIONS: dict[str, frozenset[str]] = {
    "differential": frozenset({"accept_source", "accept_target", "override", "reject"}),
    "rule": frozenset({"approve", "confirm", "reject"}),
    "mapping": frozenset({"approve", "reject"}),
    "entity_match": frozenset({"approve", "confirm", "reject"}),
    "cohort": frozenset({"approve", "reject"}),
    "break_cluster": frozenset({"accept_variance", "escalate", "reject"}),
    "reconciliation_break": frozenset({"accept_variance", "escalate", "reject"}),
    "conduct_case": frozenset({"remediate", "no_action", "reject"}),
    "go_no_go": frozenset({"go", "no_go"}),
}


class InvalidDecision(ControlPlaneViolation):
    """The decision itself is malformed (unknown value, override without an expression)."""


def validate_decision_value(item_type: str, decision: str, certified_expression: str | None) -> None:
    allowed = ALLOWED_DECISIONS.get(item_type)
    if allowed is None or decision not in allowed:
        raise InvalidDecision(f"'{decision}' is not a valid decision for {item_type}; allowed: {sorted(allowed or [])}")
    if decision == "override" and not (certified_expression or "").strip():
        raise InvalidDecision("an override must state the certified expression")


def validate_signal(signal: Signal) -> AgentIdentity:
    identity = get_identity(signal.agent)
    if signal.tier != identity.tier:
        raise ControlPlaneViolation(f"{signal.agent} is {identity.tier} but posted a {signal.tier} signal")
    if signal.signal_type not in identity.allowed_signal_types:
        raise ControlPlaneViolation(f"{signal.agent} is not authorised to post {signal.signal_type} signals")
    return identity


def validate_agent_status(agent: str, status: ItemStatus) -> None:
    identity = get_identity(agent)
    if status in HUMAN_ONLY:
        raise ControlPlaneViolation(f"{agent} tried to set '{status}'; only a named human can certify or reject")
    if status not in AGENT_STATUSES[identity.tier]:
        raise ControlPlaneViolation(f"{agent} ({identity.tier}) may not write status '{status}'")
    if status is ItemStatus.AUTOMATIC and identity.tier is not Tier.T3:
        raise ControlPlaneViolation(f"{agent} is {identity.tier}; only T3 output may be automatic")


def validate_write(agent: str, entity_type: str) -> None:
    identity = get_identity(agent)
    if entity_type not in identity.proposes:
        raise ControlPlaneViolation(f"{agent} is not authorised to write '{entity_type}'")


def validate_tool(agent: str, tool: str, *, mcp: bool = False) -> None:
    identity = get_identity(agent)
    allowed = identity.allowed_mcp_tools if mcp else identity.allowed_tools
    if tool not in allowed:
        kind = "MCP tool" if mcp else "tool"
        raise ControlPlaneViolation(f"{agent} is not authorised to use {kind} '{tool}'")


def validate_llm_purpose(agent: str, purpose: str) -> None:
    identity = get_identity(agent)
    if identity.tier is Tier.T3:
        raise ControlPlaneViolation(f"{agent} is T3 (deterministic only) and may not call an LLM")
    if purpose not in identity.llm_purposes:
        raise ControlPlaneViolation(f"{agent} may not use an LLM for '{purpose}'")
    if any(word in purpose for word in ("certify", "approve", "decide")):
        raise ControlPlaneViolation("LLMs never certify, approve or decide")


def validate_human_decision(item_type: str, role: HumanRole, decided_by: str,
                            required: HumanRole | None = None) -> None:
    """``required``: the accountable role recorded on the review task (a project may configure which role a gate
    represents); defaults to the platform's role for the item type."""
    if item_type not in DECISION_ROLES:
        raise ControlPlaneViolation(f"no human checkpoint is defined for item type '{item_type}'")
    required = required or DECISION_ROLES[item_type]
    if role is not required:
        raise ControlPlaneViolation(f"{item_type} decisions require role {required.value}, got {role.value}")
    if decided_by in AGENTS or decided_by.endswith("_agent") or decided_by.lower() in {"system", "orchestrator", "llm"}:
        raise ControlPlaneViolation("a decision must be made by a named human, not by an agent or the system")


def transition(current: ItemStatus, target: ItemStatus, *, actor_type: str) -> ItemStatus:
    """The only legal status transitions. Failure paths can never reach ``certified``."""
    if actor_type == "human":
        reviewable = (ItemStatus.PROPOSED, ItemStatus.PENDING_REVIEW, ItemStatus.UNRESOLVED, ItemStatus.AUTOMATIC)
        if current in reviewable and target in HUMAN_ONLY:
            return target
    elif actor_type == "system":
        if target is ItemStatus.UNRESOLVED and current not in HUMAN_ONLY:
            return target
        if target is ItemStatus.PENDING_REVIEW and current is ItemStatus.PROPOSED:
            return target
    elif actor_type == "agent":
        if current is ItemStatus.CANDIDATE and target in (ItemStatus.PROPOSED, ItemStatus.AUTOMATIC):
            return target
    raise ControlPlaneViolation(f"illegal transition {current} -> {target} by {actor_type}")


def self_test() -> list[tuple[str, bool]]:
    """Prove the refusal rules work before any real work runs (§40: Control Plane Self-Test)."""
    from schemas.common import new_id
    from schemas.signals import SignalType

    def refuses(fn) -> bool:
        try:
            fn()
        except ControlPlaneViolation:
            return True
        return False

    sig = Signal(signal_id=new_id("SIG"), run_id="SELFTEST", agent="cobol_agent", tier=Tier.T1,
                 signal_type=SignalType.CANDIDATE_RULES, message="self-test")
    return [
        ("accepts an authorised signal", not refuses(lambda: validate_signal(sig))),
        ("refuses a tier-mismatched signal", refuses(lambda: validate_signal(sig.model_copy(update={"tier": Tier.T3})))),
        ("refuses an unauthorised signal type",
         refuses(lambda: validate_signal(sig.model_copy(update={"signal_type": SignalType.DIFFERENTIALS})))),
        ("refuses an unregistered agent", refuses(lambda: validate_signal(sig.model_copy(update={"agent": "rogue"})))),
        ("refuses agent self-certification", refuses(lambda: validate_agent_status("php_agent", ItemStatus.CERTIFIED))),
        ("refuses automatic status for a T2 agent", refuses(lambda: validate_agent_status("mapping_agent", ItemStatus.AUTOMATIC))),
        ("refuses LLM use by a T3 agent", refuses(lambda: validate_llm_purpose("reconciler_agent", "explain_difference"))),
        ("refuses an LLM approval purpose", refuses(lambda: validate_llm_purpose("assistant_agent", "approve_mapping"))),
        ("refuses an unauthorised tool", refuses(lambda: validate_tool("cobol_agent", "evidence.write"))),
        ("refuses an unauthorised MCP tool", refuses(lambda: validate_tool("reconciler_agent", "git.read_file", mcp=True))),
        ("refuses a decision by the wrong role",
         refuses(lambda: validate_human_decision("mapping", HumanRole.ACTUARY_SME, "A. Person"))),
        ("refuses a decision attributed to an agent",
         refuses(lambda: validate_human_decision("mapping", HumanRole.DATA_STEWARD, "mapping_agent"))),
        ("refuses a system transition to certified",
         refuses(lambda: transition(ItemStatus.PROPOSED, ItemStatus.CERTIFIED, actor_type="system"))),
        ("allows timeout -> unresolved", not refuses(
            lambda: transition(ItemStatus.PENDING_REVIEW, ItemStatus.UNRESOLVED, actor_type="system"))),
    ]
