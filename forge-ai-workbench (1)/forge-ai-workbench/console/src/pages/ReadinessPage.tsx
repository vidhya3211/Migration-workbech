import type { PageProps } from "../App";
import { api, type Readiness, type Row } from "../api/client";
import { NoRun, RunPicker, useRuns } from "../components/RunPicker";
import { Chip, ErrorNote, Kpi, PageHead, Table, fmt, pct, useLoad } from "../components/ui";

// Migration readiness: the assurance agent's measured capabilities, open blockers and the human gate outcomes.
export function ReadinessPage({ pid, go }: PageProps) {
  const [runs, runId, setRunId] = useRuns(pid);
  const [r, error] = useLoad<Readiness | null>(() => (runId ? api.readiness(pid, runId) : Promise.resolve(null)), [pid, runId], null);
  if (!runs.length) return <section><PageHead title="Readiness" /><NoRun /></section>;
  const gates = Object.entries((r?.gates ?? {}) as Record<string, Row>);
  const passed = (r?.capabilities ?? []).filter((c) => c.passed).length;
  const goNoGo = (r?.go_no_go ?? {}) as Row;
  return (
    <section>
      <PageHead title="Readiness" sub="Is this migration ready to proceed? Measured, not asserted.">
        <RunPicker runs={runs} runId={runId} onChange={setRunId} />
      </PageHead>
      <ErrorNote error={error} />
      <div className="kpis">
        <Kpi label="Readiness score" value={pct(r?.readiness_score)} tone={r?.assurance_passed ? "good" : "warn"} sub={r?.assurance_passed ? "assurance passed" : "assurance not passed"} />
        <Kpi label="Capabilities passing" value={`${passed}/${r?.capabilities.length ?? 0}`} />
        <Kpi label="Blockers" value={r?.blockers.length ?? 0} tone={r?.blockers.length ? "bad" : "good"} />
        <Kpi label="Go / no-go" value={goNoGo.decision ? String(goNoGo.decision).replace("_", "-") : "pending"}
          sub={goNoGo.decided_by ? `by ${String(goNoGo.decided_by)}` : "decided by the migration lead"} />
      </div>
      {r?.blockers.length ? (
        <div className="card">
          <h3>Blockers</h3>
          <ul>{r.blockers.map((b) => <li key={b}>{b}</li>)}</ul>
          <button className="secondary" onClick={() => go("reviews")}>Open reviews</button>
        </div>
      ) : null}
      <Table title="Capabilities" rows={(r?.capabilities ?? []) as Row[]} empty="The assurance step has not run yet." columns={[
        { key: "capability", render: (c) => String(c.capability).replace(/_/g, " ") },
        { key: "passed", render: (c) => <Chip value={c.passed ? "pass" : "fail"} /> },
        { key: "metrics", render: (c) => Object.entries((c.metrics ?? {}) as Record<string, number>).map(([k, v]) => `${k} ${typeof v === "number" ? v.toFixed(2) : v}`).join(" · ") },
        { key: "thresholds", render: (c) => fmt(c.thresholds) }, { key: "notes" }]} />
      <h3>Human gates</h3>
      <Table rows={gates.map(([gate, g]) => ({ gate, ...g }))} empty="No gate has closed yet." columns={[
        { key: "gate", render: (g) => String(g.gate).replace(/_/g, " ") }, { key: "items" }, { key: "decided" }, { key: "unresolved" },
        { key: "status", render: (g) => <Chip value={Number(g.unresolved) ? "unresolved" : "complete"} /> }]} />
    </section>
  );
}
