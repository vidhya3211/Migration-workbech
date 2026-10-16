"""ATS budgets: depth, branches, tokens, time and model calls. Exhaustion is a controlled stop."""
from __future__ import annotations

import time

from pydantic import BaseModel


class ATSBudget(BaseModel):
    max_depth: int = 6
    max_branches: int = 400
    max_children: int = 6
    token_budget: int = 20000
    time_budget_seconds: float = 20
    model_call_budget: int = 5
    confidence_threshold: float = 0.55
    min_evidence: int = 1


class BudgetExhausted(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BudgetTracker:
    def __init__(self, budget: ATSBudget) -> None:
        self.budget = budget
        self.started = time.monotonic()
        self.branches = 0
        self.tokens = 0
        self.model_calls = 0
        self.max_depth_reached = 0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def charge_branch(self, depth: int) -> None:
        self.branches += 1
        self.max_depth_reached = max(self.max_depth_reached, depth)
        if self.branches > self.budget.max_branches:
            raise BudgetExhausted(f"branch budget {self.budget.max_branches} exhausted")
        if self.elapsed > self.budget.time_budget_seconds:
            raise BudgetExhausted(f"time budget {self.budget.time_budget_seconds}s exhausted")

    def charge_model_call(self, tokens: int) -> None:
        if self.model_calls + 1 > self.budget.model_call_budget:
            raise BudgetExhausted(f"model-call budget {self.budget.model_call_budget} exhausted")
        if self.tokens + tokens > self.budget.token_budget:
            raise BudgetExhausted(f"token budget {self.budget.token_budget} exhausted")
        self.model_calls += 1
        self.tokens += tokens
