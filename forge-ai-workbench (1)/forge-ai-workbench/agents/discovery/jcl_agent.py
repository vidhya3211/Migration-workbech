"""JCL agent (T1): job-control logic - step order, programs run, COND codes, PARM values - with evidence."""
from __future__ import annotations

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from agents.discovery.common import lines_excerpt, make_rule
from reasoning.deterministic.naming import canonical_name
from schemas.artefact import Artefact
from schemas.common import Tier, ReasoningMode
from schemas.provenance import CodeLocator
from schemas.rules import RuleKind
from schemas.signals import SignalType
from tools.parsers.jcl import parm_assignments, parse


@register_agent
class JclAgent(BaseAgent):
    IDENTITY = agent_identity("jcl_agent", Tier.T1, "discovery", 'Parses JCL job control, proposes rules with evidence', {S.CANDIDATE_RULES}, tools={'evidence.read'}, proposes={'rule'}, extractor=True, source_kind="jcl", dialect="jcl", handles={'jcl'})

    def run(self, artefact: Artefact, **_: object) -> AgentOutput:
        text = self.services.reader.text(artefact.blob_uri) or ""
        lines = text.split("\n")
        rules = []
        jobs = parse(text)
        for job in jobs:
            for order, step in enumerate(job.steps, 1):
                what = step.program or step.proc or "?"
                loc = CodeLocator(file=artefact.path, line_start=step.start_line, line_end=step.start_line,
                                  symbol=f"{job.name}.{step.name}")
                excerpt = lines_excerpt(lines, step.start_line, step.start_line)
                conds = [f"COND={step.cond}"] if step.cond else []
                rules.append(make_rule(
                    agent=self.name, tier=self.identity.tier, art=artefact, kind=RuleKind.SCHEDULE,
                    name=f"{job.name}.{step.name}", canonical=canonical_name(f"{job.name}_{step.name}"),
                    expression=f"RUN {what} (step {order} of {len(job.steps)})", locator=loc, excerpt=excerpt,
                    anchor=what, confidence=0.9, reasons=["deterministic parse of EXEC statement"], conditions=conds,
                    inputs=[d["dsn"] for d in step.dds if d.get("dsn")],
                    context={"job": job.name, "step": step.name, "order": order, "program": step.program,
                             "datasets": step.dds}))
                for key, value in parm_assignments(step.parm or ""):
                    rules.append(make_rule(
                        agent=self.name, tier=self.identity.tier, art=artefact, kind=RuleKind.CONSTANT,
                        name=f"{what}.{key}", canonical=canonical_name(key), expression=value, locator=loc,
                        excerpt=excerpt, anchor=key, confidence=0.75,
                        reasons=["PARM value passed to the program at run time"], context={"program": what}))
        self.note_mode(ReasoningMode.DETERMINISTIC)
        summary = {"jobs": [j.name for j in jobs], "steps": sum(len(j.steps) for j in jobs), "rules": len(rules)}
        self.signal(SignalType.CANDIDATE_RULES, f"{artefact.path}: {summary['steps']} step(s), {len(rules)} rule(s)",
                    item_refs=[r.rule_id for r in rules], data=summary, artefact_id=artefact.artefact_id)
        return AgentOutput(items={"candidates": rules}, summary=summary)
