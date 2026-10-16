import { useMemo, useState } from "react";
import type { PageProps } from "../App";
import { api, type Row } from "../api/client";
import { NoRun, RunPicker, useRuns } from "../components/RunPicker";
import { Chip, ErrorNote, Kpi, PageHead, Table, Tabs, fmt, useLoad } from "../components/ui";

// What the agents actually did on a run: each agent's execution, the trace, model calls and cost, and every
// guardrail decision. Read-only - the evidence that the governance worked.
const TABS = ["agents", "trace", "costs", "guardrails", "roster"] as const;

export function RuntimePage({ pid }: PageProps) {
  const [runs, runId, setRunId] = useRuns(pid);
  const [tab, setTab] = useState<(typeof TABS)[number]>("agents");
  const [execs, e1] = useLoad<Row[]>(() => (runId && tab === "agents" ? api.executions(runId) : Promise.resolve([])), [runId, tab], []);
  const [spans, e2] = useLoad<Row[]>(() => (runId && tab === "trace" ? api.trace(runId, 2000) : Promise.resolve([])), [runId, tab], []);
  const [costs, e3] = useLoad<Row | null>(() => (runId && tab === "costs" ? api.costs(runId) : Promise.resolve(null)), [runId, tab], null);
  const [guards, e4] = useLoad<Row[]>(() => (runId && tab === "guardrails" ? api.guardrails(runId) : Promise.resolve([])), [runId, tab], []);
  const [roster, e5] = useLoad<Row[]>(() => (tab === "roster" ? api.agents() : Promise.resolve([])), [tab], []);
  const [filter, setFilter] = useState("");
  const shownSpans = useMemo(() => (filter ? spans.filter((s) => JSON.stringify(s).toLowerCase().includes(filter.toLowerCase())) : spans), [spans, filter]);
  if (!runs.length) return <section><PageHead title="Runtime" /><NoRun /></section>;
  const ledger = (costs?.ledger ?? {}) as Row;
  return (
    <section>
      <PageHead title="Runtime" sub="Every agent execution, trace span, model call and guardrail decision for a run">
        <RunPicker runs={runs} runId={runId} onChange={setRunId} />
      </PageHead>
      <Tabs tabs={TABS} value={tab} onChange={setTab} />
      <ErrorNote error={e1 ?? e2 ?? e3 ?? e4 ?? e5} />
      {tab === "agents" && (
        <Table rows={execs} columns={[{ key: "agent_name", label: "agent" }, { key: "tier" },
          { key: "status", render: (r) => <Chip value={r.status} /> }, { key: "reasoning_mode", label: "reasoning" },
          { key: "llm", render: (r) => (r.llm_calls as unknown[] | undefined)?.length ?? 0 }, { key: "heuristic_calls", label: "heuristics" },
          { key: "ats", render: (r) => (r.ats_calls as unknown[] | undefined)?.length ?? 0 },
          { key: "duration_ms", label: "ms" }, { key: "error" }]} />
      )}
      {tab === "trace" && (
        <>
          <div className="toolbar"><input placeholder="Filter spans (agent, operation, status...)" value={filter} onChange={(e) => setFilter(e.target.value)} />
            <span className="muted small">{shownSpans.length} of {spans.length} spans</span></div>
          <Table rows={shownSpans} columns={[{ key: "start_time", label: "start" }, { key: "kind", render: (r) => <Chip value={r.kind} /> },
            { key: "operation" }, { key: "agent_id", label: "agent" }, { key: "duration_ms", label: "ms" },
            { key: "status", render: (r) => <Chip value={r.status} /> }, { key: "error" }]} />
        </>
      )}
      {tab === "costs" && (
        <>
          <div className="kpis">
            {["llm_calls", "input_tokens", "compressed_input_tokens", "output_tokens", "estimated_cost", "tool_calls"].map((k) =>
              <Kpi key={k} label={k.replace(/_/g, " ")} value={fmt(ledger[k] ?? 0)} />)}
          </div>
          <Table title="Model calls" rows={((costs?.llm_calls ?? []) as Row[])} empty="No model calls - every result in this run was deterministic or heuristic."
            columns={[{ key: "agent" }, { key: "purpose" }, { key: "model" }, { key: "prompt_version", label: "prompt" },
              { key: "input_tokens", label: "in" }, { key: "output_tokens", label: "out" }, { key: "estimated_cost", label: "cost" },
              { key: "status", render: (r) => <Chip value={r.status} /> }]} />
        </>
      )}
      {tab === "guardrails" && (
        <Table rows={guards} empty="No guardrail blocks or warnings recorded." columns={[{ key: "created_at", label: "when" }, { key: "agent" },
          { key: "stage" }, { key: "policy" }, { key: "passed", render: (r) => <Chip value={r.passed ? "pass" : "blocked"} /> },
          { key: "severity" }, { key: "reason" }]} />
      )}
      {tab === "roster" && (
        <Table rows={roster} columns={[{ key: "name" }, { key: "tier" }, { key: "group" }, { key: "job" },
          { key: "llm_purposes", label: "model use" }, { key: "uses_ats", label: "ATS" }, { key: "handles" }]} />
      )}
    </section>
  );
}

export function AuditPage({ pid }: PageProps) {
  const [entity, setEntity] = useState("");
  const [page, setPage] = useState(0);
  const limit = 200;
  const [rows, error] = useLoad<Row[]>(() => api.audit(pid, { entity_type: entity || undefined, limit: String(limit),
    offset: String(page * limit) }), [pid, entity, page], []);
  return (
    <section>
      <PageHead title="Audit trail" sub="Every governed write and every human decision in this project - append-only" />
      <ErrorNote error={error} />
      <div className="toolbar">
        <select value={entity} onChange={(e) => { setEntity(e.target.value); setPage(0); }}>
          <option value="">all entities</option>
          {["rule", "differential", "mapping", "cohort", "entity_match", "reconciliation_break", "break_cluster",
            "conduct_case", "ingest_run", "project"].map((x) => <option key={x} value={x}>{x.replace(/_/g, " ")}</option>)}
        </select>
        <button className="secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
        <span className="muted small">page {page + 1}</span>
        <button className="secondary" disabled={rows.length < limit} onClick={() => setPage(page + 1)}>Next</button>
      </div>
      <Table rows={rows} columns={[{ key: "when" }, { key: "who" }, { key: "actor_type", label: "actor" }, { key: "what" },
        { key: "entity_type", label: "entity" }, { key: "entity_id", label: "id" }, { key: "reason" }]} />
    </section>
  );
}

export function EvalsPage() {
  const [rows, error] = useLoad<Row[]>(() => api.evals(), [], []);
  return (
    <section>
      <PageHead title="Evaluations" sub="Capability scores from the evaluation suite - the release gate the platform must pass" />
      <ErrorNote error={error} />
      <Table rows={rows} empty="No evaluation results recorded yet (run: python cli.py evals --record)." columns={[
        { key: "capability" }, { key: "passed", render: (r) => <Chip value={r.passed ? "pass" : "fail"} /> },
        { key: "metrics", render: (r) => Object.entries((r.metrics ?? {}) as Record<string, number>).slice(0, 6)
          .map(([k, v]) => `${k} ${typeof v === "number" ? v.toFixed(2) : String(v)}`).join(" · ") },
        { key: "config_fingerprint", label: "configuration" }, { key: "created_at", label: "when" }]} />
    </section>
  );
}
