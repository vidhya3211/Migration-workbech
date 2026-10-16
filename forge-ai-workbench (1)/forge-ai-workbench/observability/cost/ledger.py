"""Token and cost governance (§13, §44).

A ``RunLedger`` accounts every LLM call, MCP call, tool call, ATS branch and the run's elapsed time
against the configured budgets. A violation raises ``BudgetExceeded`` - a controlled stop that the
caller turns into an explicit ``budget_stopped`` state. It is never converted into approval.
"""
from __future__ import annotations

import threading
import time
from decimal import Decimal
from functools import lru_cache

from config.settings import BudgetSettings, LLMModelSettings
from observability.logging.logger import Event, log_event


class BudgetExceeded(RuntimeError):
    def __init__(self, budget: str, used: object, limit: object) -> None:
        super().__init__(f"budget '{budget}' exceeded: {used} > {limit}")
        self.budget, self.used, self.limit = budget, used, limit


@lru_cache(maxsize=1)
def _headroom_counter():
    try:
        from headroom import count_tokens_text
        count_tokens_text("warm up", "gpt-4o")
        return count_tokens_text
    except Exception:
        return None


def count_tokens(text: str, model: str = "gpt-4o") -> int:
    """Token count via Headroom's tokenizer when installed; conservative chars/4 estimate otherwise."""
    counter = _headroom_counter()
    if counter is not None:
        try:
            return int(counter(text, model))
        except Exception:
            pass
    return max(1, (len(text) + 3) // 4) if text else 0


def estimate_cost(model: LLMModelSettings, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> Decimal:
    billable_in = max(0, input_tokens - cached_tokens)
    cost = (Decimal(billable_in) * model.input_cost_per_1k + Decimal(output_tokens) * model.output_cost_per_1k
            + Decimal(cached_tokens) * model.cached_cost_per_1k) / Decimal(1000)
    return cost.quantize(Decimal("0.000001"))


class RunLedger:
    def __init__(self, run_id: str, budgets: BudgetSettings) -> None:
        self.run_id = run_id
        self.budgets = budgets
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self.llm_calls = 0
        self.input_tokens = 0
        self.compressed_input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.mcp_calls = 0
        self.tool_calls = 0
        self.ats_branches = 0
        self.ats_max_depth = 0
        self.estimated_cost = Decimal("0")
        self.by_agent: dict[str, dict[str, float]] = {}

    def _check(self, name: str, used: object, limit: object) -> None:
        if used > limit:  # type: ignore[operator]
            log_event(Event.BUDGET_EXCEEDED, f"budget {name} exceeded", budget=name, used=str(used), limit=str(limit))
            raise BudgetExceeded(name, used, limit)

    def check_time(self) -> None:
        self._check("execution_seconds", round(time.monotonic() - self.started, 1), self.budgets.execution_seconds)

    def reserve_llm_call(self, projected_input_tokens: int) -> None:
        """Called *before* an LLM call so an over-budget call is never sent."""
        with self._lock:
            self._check("llm_calls", self.llm_calls + 1, self.budgets.llm_calls)
            self._check("input_tokens", self.input_tokens + projected_input_tokens, self.budgets.input_tokens)
            self.check_time()

    def record_llm(self, agent: str, input_tokens: int, compressed_input_tokens: int, output_tokens: int,
                   cached_tokens: int, cost: Decimal) -> None:
        with self._lock:
            self.llm_calls += 1
            self.input_tokens += input_tokens
            self.compressed_input_tokens += compressed_input_tokens
            self.output_tokens += output_tokens
            self.cached_tokens += cached_tokens
            self.estimated_cost += cost
            a = self.by_agent.setdefault(agent, {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0})
            a["llm_calls"] += 1
            a["input_tokens"] += compressed_input_tokens
            a["output_tokens"] += output_tokens
            a["cost"] += float(cost)
            self._check("output_tokens", self.output_tokens, self.budgets.output_tokens)
            self._check("estimated_cost", self.estimated_cost, self.budgets.estimated_cost)

    def record_mcp_call(self) -> None:
        with self._lock:
            self._check("mcp_calls", self.mcp_calls + 1, self.budgets.mcp_calls)
            self.mcp_calls += 1

    def record_tool_call(self) -> None:
        with self._lock:
            self._check("tool_calls", self.tool_calls + 1, self.budgets.tool_calls)
            self.tool_calls += 1

    def record_ats(self, branches: int, depth: int) -> None:
        with self._lock:
            self.ats_branches += branches
            self.ats_max_depth = max(self.ats_max_depth, depth)
            self._check("ats_branches", self.ats_branches, self.budgets.ats_branches)
            self._check("ats_depth", self.ats_max_depth, self.budgets.ats_depth)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "llm_calls": self.llm_calls, "input_tokens": self.input_tokens,
                "compressed_input_tokens": self.compressed_input_tokens, "output_tokens": self.output_tokens,
                "cached_tokens": self.cached_tokens, "mcp_calls": self.mcp_calls, "tool_calls": self.tool_calls,
                "ats_branches": self.ats_branches, "ats_max_depth": self.ats_max_depth,
                "estimated_cost": str(self.estimated_cost),
                "elapsed_seconds": round(time.monotonic() - self.started, 2),
                "by_agent": self.by_agent,
                "budgets": self.budgets.model_dump(mode="json"),
            }
