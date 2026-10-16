"""Search-policy contract: the domain plugs in how to expand, score, constrain and recognise terminals."""
from __future__ import annotations

from typing import Hashable, Protocol

from reasoning.ats.node import ATSNode, Candidate


class SearchPolicy(Protocol):
    name: str

    def root(self) -> Candidate: ...

    def expand(self, node: ATSNode) -> list[Candidate]: ...

    def score(self, node: ATSNode) -> tuple[float, float]:
        """(score used for ordering, confidence in the hypothesis)."""
        ...

    def violates(self, node: ATSNode) -> str | None:
        """Deterministic constraint check - a reason string prunes the node."""
        ...

    def is_terminal(self, node: ATSNode) -> bool: ...

    def state_key(self, node: ATSNode) -> Hashable: ...
