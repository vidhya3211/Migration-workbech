import { useMemo, useState } from "react";
import type { PageProps } from "../App";
import { api, type Graph } from "../api/client";
import { NoRun, RunPicker, useRuns } from "../components/RunPicker";
import { ErrorNote, PageHead, Table, Tabs, useLoad } from "../components/ui";

// Dependencies found in the evidence: batch jobs -> programs -> datasets (JCL), business rules feeding other
// rules, and source entities mapped to target entities. Drawn as a layered graph (no external libraries).
const COLORS: Record<string, string> = { job: "#7b4fc9", program: "#2f7dd1", dataset: "#1f9d77", rule: "#c97a1f", entity: "#c43d6b" };
const VIEWS = ["graph", "list"] as const;

export function DependenciesPage({ pid }: PageProps) {
  const [runs, runId, setRunId] = useRuns(pid);
  const [view, setView] = useState<(typeof VIEWS)[number]>("graph");
  const [kinds, setKinds] = useState<Set<string>>(new Set(Object.keys(COLORS)));
  const [focus, setFocus] = useState<string | null>(null);
  const [graph, error] = useLoad<Graph | null>(() => (runId ? api.dependencies(pid, runId, 600) : Promise.resolve(null)), [pid, runId], null);
  const filtered = useMemo(() => {
    if (!graph) return null;
    const nodes = graph.nodes.filter((n) => kinds.has(n.kind));
    const ids = new Set(nodes.map((n) => n.id));
    let edges = graph.edges.filter((e) => ids.has(e.from) && ids.has(e.to));
    if (focus) {
      const near = new Set([focus]);
      edges = edges.filter((e) => e.from === focus || e.to === focus);
      edges.forEach((e) => { near.add(e.from); near.add(e.to); });
      return { nodes: nodes.filter((n) => near.has(n.id)), edges };
    }
    return { nodes: nodes.filter((n) => edges.some((e) => e.from === n.id || e.to === n.id)), edges };
  }, [graph, kinds, focus]);
  if (!runs.length) return <section><PageHead title="Dependencies" /><NoRun /></section>;
  const toggle = (k: string) => setKinds((s) => { const n = new Set(s); if (n.has(k)) n.delete(k); else n.add(k); return n; });
  const label = (id: string) => graph?.nodes.find((n) => n.id === id)?.label ?? id;
  return (
    <section>
      <PageHead title="Dependencies" sub="What runs what, what reads and writes which data, and how source maps to target">
        <RunPicker runs={runs} runId={runId} onChange={(id) => { setRunId(id); setFocus(null); }} />
      </PageHead>
      <ErrorNote error={error} />
      <div className="toolbar">
        <Tabs tabs={VIEWS} value={view} onChange={setView} />
        <div className="legend">
          {Object.entries(COLORS).map(([k, c]) => (
            <label key={k} style={{ cursor: "pointer" }}>
              <input type="checkbox" style={{ width: "auto", marginRight: 4 }} checked={kinds.has(k)} onChange={() => toggle(k)} />
              <i style={{ background: c }} />{k}
            </label>
          ))}
        </div>
        {focus && <button className="secondary" onClick={() => setFocus(null)}>Clear focus: {label(focus)}</button>}
        {graph?.truncated && <span className="muted small">showing the {graph.edges.length} strongest of {graph.total_edges} links</span>}
      </div>
      {filtered && filtered.edges.length === 0 && <div className="card empty">No dependencies of the selected kinds were found in this run.</div>}
      {filtered && filtered.edges.length > 0 && view === "graph" && <GraphView nodes={filtered.nodes} edges={filtered.edges} onFocus={setFocus} />}
      {filtered && view === "list" && (
        <Table rows={filtered.edges.map((e) => ({ from: label(e.from), kind: e.kind.replace(/_/g, " "), to: label(e.to), count: e.count }))}
          columns={[{ key: "from" }, { key: "kind", label: "relationship" }, { key: "to" }, { key: "count", label: "links" }]} />
      )}
    </section>
  );
}

function GraphView({ nodes, edges, onFocus }: { nodes: Graph["nodes"]; edges: Graph["edges"]; onFocus: (id: string) => void }) {
  const layout = useMemo(() => {
    // longest-path layering (cycles are cut by a depth cap), then nodes spread evenly within each layer
    const out = new Map<string, string[]>();
    edges.forEach((e) => out.set(e.from, [...(out.get(e.from) ?? []), e.to]));
    const depth = new Map<string, number>(nodes.map((n) => [n.id, 0]));
    for (let pass = 0; pass < 8; pass++) {
      let changed = false;
      edges.forEach((e) => {
        const d = (depth.get(e.from) ?? 0) + 1;
        if (d > (depth.get(e.to) ?? 0) && d < 8) { depth.set(e.to, d); changed = true; }
      });
      if (!changed) break;
    }
    const layers = new Map<number, string[]>();
    nodes.forEach((n) => { const d = depth.get(n.id) ?? 0; layers.set(d, [...(layers.get(d) ?? []), n.id]); });
    const cols = Math.max(...layers.keys()) + 1;
    const rows = Math.max(...[...layers.values()].map((l) => l.length));
    const W = Math.max(900, cols * 240);
    const H = Math.max(400, rows * 34 + 40);
    const pos = new Map<string, { x: number; y: number }>();
    layers.forEach((ids, d) => {
      ids.sort();
      ids.forEach((id, i) => pos.set(id, { x: 90 + d * ((W - 180) / Math.max(1, cols - 1)), y: 20 + (i + 0.5) * ((H - 40) / ids.length) }));
    });
    return { pos, W, H };
  }, [nodes, edges]);
  const kindOf = new Map(nodes.map((n) => [n.id, n]));
  return (
    <div style={{ overflow: "auto" }}>
      <svg className="graph" viewBox={`0 0 ${layout.W} ${layout.H}`} style={{ minWidth: Math.min(layout.W, 1600), height: Math.min(layout.H, 900) }}>
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#999" />
          </marker>
        </defs>
        {edges.map((e, i) => {
          const a = layout.pos.get(e.from);
          const b = layout.pos.get(e.to);
          if (!a || !b) return null;
          const mx = (a.x + b.x) / 2;
          return <path key={i} d={`M ${a.x + 6} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${b.x - 8} ${b.y}`} fill="none" stroke="#9a94ad"
            strokeOpacity={0.5} strokeWidth={Math.min(4, 1 + Math.log2(e.count))} markerEnd="url(#arrow)"><title>{e.kind} ({e.count})</title></path>;
        })}
        {[...layout.pos.entries()].map(([id, p]) => {
          const n = kindOf.get(id);
          return (
            <g key={id} transform={`translate(${p.x},${p.y})`} style={{ cursor: "pointer" }} onClick={() => onFocus(id)}>
              <circle r={6} fill={COLORS[n?.kind ?? ""] ?? "#888"} />
              <text x={10} y={4}>{(n?.label ?? id).slice(0, 34)}</text>
              <title>{n?.kind}: {n?.label}</title>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
