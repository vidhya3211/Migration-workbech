"""ATS execution trace: every expansion, pruning and selection, persisted with the run (§7, §47)."""
from __future__ import annotations

from pydantic import Field

from reasoning.ats.node import ATSNode
from schemas.common import WorkbenchModel


class ATSEvent(WorkbenchModel):
    seq: int
    event: str                 # expanded | pruned | terminal | selected | budget_stop
    node_id: str
    detail: str


class ATSResult(WorkbenchModel):
    ats_run_id: str
    policy: str
    best: ATSNode | None
    alternatives: list[ATSNode] = Field(default_factory=list)
    nodes: list[ATSNode] = Field(default_factory=list)
    events: list[ATSEvent] = Field(default_factory=list)
    expanded: int = 0
    pruned: int = 0
    branches: int = 0
    max_depth_reached: int = 0
    model_calls: int = 0
    tokens: int = 0
    elapsed_ms: float = 0
    termination_reason: str = ""

    def summary(self) -> dict:
        return {"ats_run_id": self.ats_run_id, "policy": self.policy,
                "best": self.best.hypothesis if self.best else None,
                "best_confidence": self.best.confidence if self.best else 0.0,
                "alternatives": [a.hypothesis for a in self.alternatives[:5]], "expanded": self.expanded,
                "pruned": self.pruned, "branches": self.branches, "max_depth": self.max_depth_reached,
                "model_calls": self.model_calls, "tokens": self.tokens, "elapsed_ms": round(self.elapsed_ms, 2),
                "termination_reason": self.termination_reason}
