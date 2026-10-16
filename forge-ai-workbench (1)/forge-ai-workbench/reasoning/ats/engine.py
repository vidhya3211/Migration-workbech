"""Abstract Tree Search engine (§7): bounded best-first exploration of candidate hypotheses.

ATS decides *which alternative to explore*; LangGraph decides *what happens next* in the workflow.
Search is never unbounded: depth, branches, tokens, time and model calls are budgeted, states are
de-duplicated, constraint violations are pruned deterministically, terminals need evidence, and the
search stops once a terminal hypothesis reaches the confidence threshold and nothing open can beat it.
The winning hypothesis is still only a *candidate* - it goes through validation, the guard and humans.
"""
from __future__ import annotations

import heapq
import itertools
from typing import Hashable

from guardrails.pipeline import GuardrailContext, GuardrailPipeline, GuardrailViolation, Stage
from guardrails.policy.policies import ReasoningBudget
from observability.cost.ledger import BudgetExceeded, RunLedger
from observability.logging.logger import Event, log_event
from observability.tracing.tracer import SpanKind, get_tracer
from reasoning.ats.budget import ATSBudget, BudgetExhausted, BudgetTracker
from reasoning.ats.node import ATSNode, Candidate, NodeStatus
from reasoning.ats.pruning import beam, prune_reason
from reasoning.ats.search_policy import SearchPolicy
from reasoning.ats.trace import ATSEvent, ATSResult
from schemas.common import new_id


class ATSEngine:
    def __init__(self, budget: ATSBudget, guardrails: GuardrailPipeline | None = None,
                 ledger: RunLedger | None = None, run_id: str = "-", agent: str = "-") -> None:
        self.budget = budget
        self.guardrails = guardrails
        self.ledger = ledger
        self.run_id = run_id
        self.agent = agent

    def search(self, policy: SearchPolicy) -> ATSResult:
        result = ATSResult(ats_run_id=new_id("ATS"), policy=policy.name, best=None)
        tracker = BudgetTracker(self.budget)
        seq = itertools.count()
        seen: set[Hashable] = set()
        frontier: list[tuple[float, int, ATSNode]] = []
        terminals: list[ATSNode] = []
        log_event(Event.ATS_STARTED, policy=policy.name, ats_run_id=result.ats_run_id)

        def event(kind: str, node: ATSNode, detail: str) -> None:
            result.events.append(ATSEvent(seq=len(result.events), event=kind, node_id=node.node_id, detail=detail[:300]))

        def make(c: Candidate, parent: ATSNode | None) -> ATSNode:
            node = ATSNode(node_id=f"{result.ats_run_id}-N{next(seq)}", parent_node_id=parent.node_id if parent else None,
                           hypothesis=c.hypothesis, evidence=c.evidence, actions=c.actions, constraints=c.constraints,
                           depth=(parent.depth + 1) if parent else 0, state=c.state)
            node.score, node.confidence = policy.score(node)
            result.nodes.append(node)
            return node

        with get_tracer().span(SpanKind.ATS, policy.name, ats_run_id=result.ats_run_id) as span:
            root = make(policy.root(), None)
            seen.add(policy.state_key(root))
            heapq.heappush(frontier, (-root.score, 0, root))
            try:
                while frontier:
                    _, _, node = heapq.heappop(frontier)
                    best = max(terminals, key=lambda n: n.confidence, default=None)
                    if best is not None and best.confidence >= self.budget.confidence_threshold and best.score >= node.score:
                        result.termination_reason = "confident terminal found; no open node can beat it"
                        break
                    if policy.is_terminal(node):
                        node.status = NodeStatus.TERMINAL
                        terminals.append(node)
                        event("terminal", node, f"score {node.score} confidence {node.confidence}")
                        continue
                    if node.depth + 1 > self.budget.max_depth:
                        node.status, node.termination_reason = NodeStatus.PRUNED, "max depth reached"
                        result.pruned += 1
                        event("pruned", node, "max depth reached")
                        continue
                    self._check_reasoning_budget(tracker, node.depth + 1)
                    children: list[ATSNode] = []
                    for cand in policy.expand(node):
                        tracker.charge_branch(node.depth + 1)
                        child = make(cand, node)
                        reason = prune_reason(child, policy, seen, self.budget)
                        if reason:
                            child.status, child.termination_reason = NodeStatus.PRUNED, reason
                            result.pruned += 1
                            event("pruned", child, reason)
                            log_event(Event.ATS_NODE_PRUNED, reason, node_id=child.node_id)
                            continue
                        children.append(child)
                    kept, dropped = beam(children, self.budget.max_children)
                    for d in dropped:
                        d.status, d.termination_reason = NodeStatus.PRUNED, "outside beam width"
                        result.pruned += 1
                        event("pruned", d, "outside beam width")
                    for child in kept:
                        heapq.heappush(frontier, (-child.score, next(seq), child))
                    node.status = NodeStatus.EXPANDED
                    result.expanded += 1
                    event("expanded", node, f"{len(kept)} child(ren) kept, {len(dropped)} beamed out")
                    log_event(Event.ATS_NODE_EXPANDED, node_id=node.node_id, depth=node.depth, children=len(kept))
                else:
                    result.termination_reason = result.termination_reason or "search space exhausted"
            except BudgetExhausted as exc:
                result.termination_reason = f"budget stop: {exc.reason}"
                event("budget_stop", root, exc.reason)
            ranked = sorted(terminals, key=lambda n: (-n.confidence, -n.score, n.node_id))
            if ranked and ranked[0].confidence >= self.budget.confidence_threshold:
                result.best = ranked[0]
                result.best.status = NodeStatus.SELECTED
                event("selected", ranked[0], ranked[0].hypothesis)
            elif ranked:
                result.termination_reason += "; best terminal below confidence threshold - no selection"
            result.alternatives = ranked[1:6] if result.best else ranked[:5]
            result.branches = tracker.branches
            result.max_depth_reached = tracker.max_depth_reached
            result.model_calls, result.tokens = tracker.model_calls, tracker.tokens
            result.elapsed_ms = tracker.elapsed * 1000
            span.set(expanded=result.expanded, pruned=result.pruned, branches=result.branches,
                     depth=result.max_depth_reached, selected=bool(result.best))
        if self.ledger is not None:
            try:
                self.ledger.record_ats(result.branches, result.max_depth_reached)
            except BudgetExceeded:
                result.termination_reason += "; run-level ATS budget exceeded"
                raise
        log_event(Event.ATS_COMPLETED, result.termination_reason, policy=policy.name, expanded=result.expanded,
                  pruned=result.pruned, selected=bool(result.best))
        return result

    def _check_reasoning_budget(self, tracker: BudgetTracker, depth: int) -> None:
        if self.guardrails is None:
            if depth > self.budget.max_depth:
                raise BudgetExhausted(f"depth {depth} > {self.budget.max_depth}")
            return
        subject = ReasoningBudget(depth=depth, branches=tracker.branches, max_depth=self.budget.max_depth,
                                  max_branches=self.budget.max_branches, elapsed_s=tracker.elapsed,
                                  time_budget_s=self.budget.time_budget_seconds)
        try:
            self.guardrails.run(Stage.REASONING_BUDGET, subject, GuardrailContext(self.run_id, self.agent))
        except GuardrailViolation as exc:
            raise BudgetExhausted(str(exc)) from exc
