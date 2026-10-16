"""Deterministic pruning: constraint violations, duplicate states, missing evidence, beam width."""
from __future__ import annotations

from typing import Hashable

from reasoning.ats.budget import ATSBudget
from reasoning.ats.node import ATSNode
from reasoning.ats.search_policy import SearchPolicy


def prune_reason(node: ATSNode, policy: SearchPolicy, seen: set[Hashable], budget: ATSBudget) -> str | None:
    if node.depth > budget.max_depth:
        return f"depth {node.depth} exceeds max_depth {budget.max_depth}"
    violation = policy.violates(node)
    if violation:
        return f"constraint: {violation}"
    key = policy.state_key(node)
    if key in seen:
        return "duplicate state"
    seen.add(key)
    if policy.is_terminal(node) and len(node.evidence) < budget.min_evidence:
        return f"terminal hypothesis without the required evidence ({len(node.evidence)} < {budget.min_evidence})"
    return None


def beam(children: list[ATSNode], width: int) -> tuple[list[ATSNode], list[ATSNode]]:
    ranked = sorted(children, key=lambda n: (-n.score, n.node_id))
    return ranked[:width], ranked[width:]
