import { useState } from "react";
import type { PageProps } from "../App";
import { api, bytes, type Row } from "../api/client";
import { NoRun, RunPicker, useRuns } from "../components/RunPicker";
import { Chip, ErrorNote, Kpi, PageHead, Table, Tabs, fmt, useLoad } from "../components/ui";

// Everything the workbench found in the uploaded bundles: classified artefacts, data extracts and their profile.
const TABS = ["artefacts", "data profile", "live vs archive", "change data"] as const;

export function AssetsPage({ pid }: PageProps) {
  const [runs, runId, setRunId] = useRuns(pid);
  const [tab, setTab] = useState<(typeof TABS)[number]>("artefacts");
  const [page, setPage] = useState(0);
  const [filter, setFilter] = useState("");
  const limit = 200;
  const [artefacts, error] = useLoad<Row[]>(() => (runId && tab === "artefacts" ? api.artefacts(runId, limit, page * limit) : Promise.resolve([])),
    [runId, tab, page], []);
  const [profiles] = useLoad<Row[]>(() => (runId && tab === "data profile" ? api.list(pid, "dq/profiles", { run_id: runId }) : Promise.resolve([])),
    [pid, runId, tab], []);
  const [rec, setRec] = useState<string>("");
  const [archive] = useLoad<{ total: number; items: Row[] }>(() => (runId && tab === "live vs archive"
    ? api.liveArchive(pid, runId, rec || undefined, 100) : Promise.resolve({ total: 0, items: [] })), [pid, runId, tab, rec], { total: 0, items: [] });
  const [summary] = useLoad(() => (runId && (tab === "live vs archive" || tab === "change data") ? api.waterfall(pid, runId) : Promise.resolve(null)),
    [pid, runId, tab], null as Awaited<ReturnType<typeof api.waterfall>> | null);
  if (!runs.length) return <section><PageHead title="Assets" /><NoRun /></section>;
  const la = (summary?.live_archive ?? null) as Row | null;
  const shown = filter ? artefacts.filter((a) => JSON.stringify(a).toLowerCase().includes(filter.toLowerCase())) : artefacts;
  const types: Record<string, number> = {};
  artefacts.forEach((a) => { types[String(a.artefact_type)] = (types[String(a.artefact_type)] ?? 0) + 1; });
  return (
    <section>
      <PageHead title="Assets" sub="Source code, schemas, documents and data extracts discovered in the uploaded bundles">
        <RunPicker runs={runs} runId={runId} onChange={(id) => { setRunId(id); setPage(0); }} />
      </PageHead>
      <Tabs tabs={TABS} value={tab} onChange={setTab} />
      <ErrorNote error={error} />
      {tab === "artefacts" && (
        <>
          <div className="kpis">
            {Object.entries(types).sort((a, b) => b[1] - a[1]).slice(0, 6).map(([t, n]) => <Kpi key={t} label={t.replace(/_/g, " ")} value={n} />)}
          </div>
          <div className="toolbar">
            <input placeholder="Filter by path, type, system..." value={filter} onChange={(e) => setFilter(e.target.value)} />
            <span className="muted small">{shown.length} shown (page {page + 1})</span>
            <button className="secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
            <button className="secondary" disabled={artefacts.length < limit} onClick={() => setPage(page + 1)}>Next</button>
          </div>
          <Table rows={shown} columns={[
            { key: "path" }, { key: "system_name", label: "system" }, { key: "system_role", label: "role" },
            { key: "artefact_type", label: "type", render: (r) => <Chip value={r.artefact_type} /> }, { key: "language" },
            { key: "size", render: (r) => bytes(r.size_bytes as number) }, { key: "line_count", label: "lines" },
            { key: "classification" }, { key: "analysis_status", label: "analysis", render: (r) => <Chip value={r.analysis_status} /> }]} />
        </>
      )}
      {tab === "change data" && (
        <>
          <Table title="Change files applied to their base extracts (in sequence order)" rows={summary?.change_data ?? []}
            empty="No change-data files configured (heuristics.change_data)." columns={[
              { key: "changes", label: "change file" }, { key: "base", label: "applied to" }, { key: "insert" }, { key: "update" },
              { key: "delete" }, { key: "conflicts", render: (r) => <Chip value={Number(r.conflicts) ? `${String(r.conflicts)} conflict(s)` : "clean"} /> },
              { key: "rows_after", label: "records after" }, { key: "error" }]} />
          {(summary?.change_data ?? []).filter((c) => ((c.conflict_details as Row[]) ?? []).length).map((c) => (
            <Table key={String(c.changes)} title={`Conflicts in ${String(c.changes)} - not applied`} rows={c.conflict_details as Row[]}
              columns={[{ key: "change_line", label: "line" }, { key: "key", label: "record" }, { key: "reason" }]} />
          ))}
        </>
      )}
      {tab === "data profile" && (
        <Table rows={profiles} empty="No data extracts profiled in this run." columns={[
          { key: "entity" }, { key: "column" }, { key: "inferred_type", label: "type" }, { key: "row_count", label: "rows" },
          { key: "null_count", label: "nulls" }, { key: "distinct_count", label: "distinct" },
          { key: "min_value", label: "min" }, { key: "max_value", label: "max" },
          { key: "pii", render: (r) => (r.pii ? <Chip value="PII - tokenised" /> : <span className="muted">-</span>) },
          { key: "top_values", label: "most common", render: (r) => fmt(r.top_values) }]} />
      )}
      {tab === "live vs archive" && (
        <>
          <div className="toolbar">
            <select value={rec} onChange={(e) => setRec(e.target.value)}>
              <option value="">all recommendations</option><option value="likely_live">likely live</option>
              <option value="likely_archive">likely archive</option><option value="review">review</option>
            </select>
            <span className="muted">{archive.total.toLocaleString()} record(s)</span>
          </div>
          {la && Array.isArray(la.threshold_curve) && (
            <Table title={<>Threshold curve <span className="muted small">how many records stay live at each cut-off
              {Number(la.overridden) ? ` · ${String(la.overridden)} lifted by override (${(la.override_rules as string[]).join(", ")})` : ""}</span></>}
              rows={la.threshold_curve as Row[]} columns={[{ key: "cutoff", label: "live if probability ≥" }, { key: "live" }, { key: "archive" }]} />
          )}
          <h3>Records</h3>
          <Table rows={archive.items} columns={[{ key: "record_ref", label: "record" },
            { key: "recommendation", render: (r) => <Chip value={r.recommendation} /> },
            { key: "live_probability", label: "live probability" }, { key: "components", label: "signals" }]} />
        </>
      )}
    </section>
  );
}
