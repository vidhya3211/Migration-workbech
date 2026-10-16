"""Story agent (T2): turns certified rules into testable user stories for the build team (UC14).

Acceptance criteria and worked examples are computed deterministically by executing the certified rules -
the examples are proofs, not prose. Where a model is permitted it rewrites the story as a readable narrative
with Given/When/Then criteria - every figure must come from the certified rule or the computed examples
(grounding guardrail), and the computed criteria are always kept alongside. Creating the work items in Azure
DevOps goes through the governed MCP tool, which requires a recorded human approval.
"""
from __future__ import annotations

from collections import defaultdict

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from pydantic import BaseModel, Field

from agents.base import AgentOutput, BaseAgent
from reasoning.deterministic.equivalence import Program, domain_for, simulate, vectors
from reasoning.deterministic.programs import build_programs
from schemas.build import UserStory
from schemas.common import Tier, ReasoningMode, stable_id
from schemas.rules import NormalisedRule, RuleKind
from schemas.signals import SignalType


class StoryDraft(BaseModel):
    title: str
    narrative: str
    acceptance_criteria: list[str] = Field(default_factory=list)


@register_agent
class StoryAgent(BaseAgent):
    IDENTITY = agent_identity("story_agent", Tier.T2, "discovery", 'Turns certified rules into testable user stories', {S.USER_STORIES}, tools={'deterministic.expression'}, mcp={'devops.create_work_item'}, proposes={'story'}, llm={'user_story'})

    def run(self, certified: list[NormalisedRule], **_: object) -> AgentOutput:
        programs = build_programs(certified)
        by_name: dict[str, list[NormalisedRule]] = defaultdict(list)
        for n in certified:
            if n.kind in (RuleKind.CALCULATION, RuleKind.CONSTANT, RuleKind.ASSIGNMENT):
                by_name[n.canonical_name].append(n)
        stories = []
        for name, rules in sorted(by_name.items()):
            criteria, examples = [], []
            for r in rules:
                when = " AND ".join(r.condition_signature) if r.condition_signature else "always"
                criteria.append(f"Given {when}, when {name} is calculated, then it equals {r.resolved_expression} "
                                f"(certified from {r.evidence.locator.file})")
            prog = next((p for p in programs.values() if name in p.assigned()), None)
            if prog is not None:
                examples = self._examples(prog, name)
            for ex in examples:
                criteria.append(f"Example: with {ex['inputs'] or 'no inputs'} -> {name} = {ex['value']}")
            first = rules[0]
            title = f"Implement certified rule: {name.replace('_', ' ').lower()}"
            narrative = (f"As the migration build team, we need {name.replace('_', ' ').lower()} implemented exactly as "
                         f"certified, so that migrated policies behave as the business signed off.")
            meta = None
            facts = "\n".join([f"{r.resolved_expression} when {' AND '.join(r.condition_signature) or 'always'}"
                                for r in rules] + [f"example {ex['inputs']} -> {ex['value']}" for ex in examples])
            draft, meta = self.narrate("user_story", {
                "rule": name, "expression": "; ".join(r.resolved_expression for r in rules),
                "conditions": "; ".join(" AND ".join(r.condition_signature) or "always" for r in rules),
                "certified_from": ", ".join(sorted({r.evidence.locator.file for r in rules})),
                "examples": str(examples)}, facts, StoryDraft,
                text_of=lambda p: " ".join([p.title, p.narrative, *p.acceptance_criteria]))
            if draft is not None:
                title, narrative = draft.title, draft.narrative
                criteria = draft.acceptance_criteria + [f"(computed) {c}" for c in criteria]
            stories.append(UserStory(
                story_id=stable_id("STY", self.run_id, name), run_id=self.run_id, rule_id=first.rule_id,
                title=title, narrative=narrative, acceptance_criteria=criteria, test_vectors=examples,
                evidence=[r.evidence for r in rules], model_assisted=meta))
        self.note_mode(ReasoningMode.DETERMINISTIC)
        self.signal(SignalType.USER_STORIES, f"{len(stories)} testable user stor(ies) from certified rules",
                    item_refs=[s.story_id for s in stories],
                    data={"work_items": "not created automatically - raising one via devops.create_work_item (MCP) needs a recorded human approval"})
        return AgentOutput(items={"stories": stories})

    @staticmethod
    def _examples(prog: Program, name: str) -> list[dict]:
        inputs = prog.referenced() - prog.assigned()
        out = []
        for vec in vectors(domain_for([prog], inputs), limit=64)[:3]:
            env = simulate(prog, vec)
            if name in env:
                v = env[name]
                out.append({"inputs": {k: str(x) for k, x in vec.items()}, "value": format(v.normalize(), "f")
                            if hasattr(v, "normalize") else str(v)})
        return out
