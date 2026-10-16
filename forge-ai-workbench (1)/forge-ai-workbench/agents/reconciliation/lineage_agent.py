"""Lineage agent (T3): traces the exact path a value took - source cell -> mapping -> transformation ->
expected value -> target cell -> cluster -> certified rule (with its source line fetched through the
governed Git MCP tool). Mechanical; every step is a reference, never a raw customer value in telemetry.

Traced for a bounded sample of breaks per cluster (``scale.lineage_breaks_per_cluster``): every break in a
cluster shares the same signature and therefore the same path.
"""
from __future__ import annotations

from blackboard.identity import agent_identity, register_agent
from schemas.signals import SignalType as S
from agents.base import AgentOutput, BaseAgent
from schemas.common import Tier, ReasoningMode
from schemas.differential import Differential
from schemas.mapping import MappingProposal
from schemas.provenance import locator_label
from schemas.recon import BreakCluster, LineageStep, LineageTrace, ReconciliationBreak
from schemas.signals import SignalLevel, SignalType
from tools.mcp.gateway import MCPError


@register_agent
class LineageAgent(BaseAgent):
    IDENTITY = agent_identity("lineage_agent", Tier.T3, "reconciliation", 'Traces the exact path a value took', {S.LINEAGE}, mcp={'git.read_file'})

    def run(self, breaks: list[ReconciliationBreak], clusters: list[BreakCluster], mappings: list[MappingProposal],
            differentials: list[Differential], **_: object) -> AgentOutput:
        maps = {m.mapping_id: m for m in mappings}
        cluster_of = {bid: c for c in clusters for bid in c.break_ids}
        by_cluster = {c.cluster_id: c for c in clusters}
        diffs = {d.differential_id: d for d in differentials}
        code_line: dict[str, str] = {}
        traces: list[LineageTrace] = []
        mcp_failures = 0
        for b in breaks:
            steps = []
            for ev in b.evidence:
                steps.append(LineageStep(stage=f"{ev.system.system_role.value} record",
                                         description=f"{ev.system.system_name}: {locator_label(ev.locator)}",
                                         reference=ev.excerpt))
            m = maps.get(b.mapping_id or "")
            if m is not None:
                steps.append(LineageStep(stage="certified mapping", reference=m.mapping_id,
                                         description=f"{m.source.entity}.{m.source.field} -> {m.target.entity}.{m.target.field}: "
                                                     f"{m.transformation.description}"))
            c = by_cluster.get(b.cluster_id or "") or cluster_of.get(b.break_id)
            if c is not None:
                steps.append(LineageStep(stage="break cluster", reference=c.cluster_id, description=c.hypothesis[:300]))
                d = diffs.get(c.linked_differential_id or "")
                if d is not None and d.evidence:
                    ev = d.evidence[0]
                    if d.differential_id not in code_line:
                        code_line[d.differential_id] = self._fetch(ev)
                        mcp_failures += code_line[d.differential_id].startswith("(unavailable")
                    steps.append(LineageStep(stage="certified rule", reference=d.differential_id,
                                             description=f"{d.canonical_name} at {locator_label(ev.locator)}: "
                                                         f"{code_line[d.differential_id][:200]}"))
            traces.append(LineageTrace(break_id=b.break_id, steps=steps))
        self.note_mode(ReasoningMode.DETERMINISTIC)
        self.signal(SignalType.LINEAGE, f"lineage traced for {len(traces)} break(s)",
                    item_refs=[t.break_id for t in traces], level=SignalLevel.WARN if mcp_failures else SignalLevel.INFO,
                    data={"mcp_lookups": len(code_line), "mcp_failures": mcp_failures})
        return AgentOutput(items={"lineage": traces})

    def _fetch(self, ev) -> str:
        art = self.services.artefacts(self.run_id).get(ev.artefact_id)
        loc = ev.locator
        if art is None or not hasattr(loc, "line_start"):
            return ev.excerpt.strip()
        try:
            res = self.mcp("git.read_file", {"uri": art.blob_uri, "line_start": loc.line_start, "line_end": loc.line_end})
            return res.text.strip() if res.status == "ok" else f"(unavailable: {res.status})"
        except MCPError as exc:
            return f"(unavailable: {exc})"
