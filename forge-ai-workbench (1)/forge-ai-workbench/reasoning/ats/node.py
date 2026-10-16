"""ATS tree node (§7). Every field the spec requires, plus the domain ``state`` used for duplicate detection."""
from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from schemas.common import WorkbenchModel


class NodeStatus(StrEnum):
    OPEN = "open"
    EXPANDED = "expanded"
    PRUNED = "pruned"
    TERMINAL = "terminal"
    SELECTED = "selected"


class ATSNode(WorkbenchModel):
    node_id: str
    parent_node_id: str | None
    hypothesis: str
    evidence: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    score: float = 0.0
    confidence: float = 0.0
    depth: int = 0
    status: NodeStatus = NodeStatus.OPEN
    termination_reason: str | None = None
    state: dict = Field(default_factory=dict)


class Candidate(WorkbenchModel):
    """A proposed child produced by a search policy (deterministic, heuristic or LLM expansion)."""

    hypothesis: str
    state: dict
    evidence: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    source: str = "deterministic"   # deterministic | heuristic | llm
