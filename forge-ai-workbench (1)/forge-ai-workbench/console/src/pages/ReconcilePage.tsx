import { useState } from "react";
import type { PageProps } from "../App";
import { api, type Row } from "../api/client";
import { EvidenceDrawer } from "../components/EvidenceDrawer";
import { NoRun, RunPicker, useRuns } from "../components/RunPicker";
import { Assisted, Chip, ErrorNote, PageHead, Table, Tabs, fmt, useLoad } from "../components/ui";

// Reconciliation: source vs loaded target, breaks grouped into explainable clusters, and customer outcomes.
const TABS = ["break clusters", "breaks", "customer outcomes", "controls"] as const;

export function ReconcilePage({ pid }: PageProps) {
  const [runs, runId, setRunId] = useRuns(pid);
  const [tab, setTab] = useState<(typeof TABS)[number]>("break clusters");
  const [cluster, setCluster] = useState<Row | null>(null);
  const [band, setBand] = useState("");
  const [page, setPage] = useState(0);
  const [evidence, setEvidence] = useState<{ type: string; id: string } | null>(null);
  const [caseRow, setCaseRow] = useState<Row | null>(null);
  const limit = 100;
  const [clusters, e1] = useLoad<Row[]>(() => (runId ? api.list(pid, "reconciliation/clusters", { run_id: runId }) : Promise.resolve([])), [pid, runId], []);
  const [breaks, e2] = useLoad<Row[]>(() => (runId && tab === "breaks" ? api.list(pid, "reconciliation/breaks",
    { run_id: runId, cluster_id: cluster ? String(cluster.cluster_id) : undefined, limit, offset: page * limit }) : Promise.resolve([])),
  [pid, runId, tab, cluster, page], []);
  const [cases, e3] = useLoad<Row[]>(() => (runId && tab === "customer outcomes" ? api.list(pid, "conduct-cases",
    { run_id: runId, band: band || undefined, limit, offset: page * limit }) : Promise.resolve([])), [pid, runId, tab, band, page], []);
  if (!runs.length) return <section><PageHead title="Reconcile" /><NoRun /></section>;
  return (
    <section>
      <PageHead title="Reconcile" sub="Every source record against the loaded target - breaks explained, customer impact assessed">
        <RunPicker runs={runs} runId={runId} onChange={(id) => { setRunId(id); setCluster(null); setPage(0); }} />
      </PageHead>
      <Tabs tabs={TABS} value={tab} onChange={(t) => { setTab(t); setPage(0); }} />
      <ErrorNote error={e1 ?? e2 ?? e3} />
      {tab === "break clusters" && (
        <>
          <Table rows={clusters} onRow={setCluster} empty="No reconciliation breaks - source and target agree." columns={[
            { key: "signature" }, { key: "field" }, { key: "count", label: "breaks" }, { key: "total_delta", label: "total difference" },
            { key: "shape", render: (r) => <ShapeChip shape={(r.body as Row)?.shape as Row | undefined} /> },
            { key: "cause", render: (r) => fmt((r.body as Row)?.hypothesis) },
            { key: "status", render: (r) => <Chip value={r.status} /> }]} />
          {cluster && <ClusterDetail row={cluster} onBreaks={() => { setTab("breaks"); setPage(0); }} />}
        </>
      )}
      {tab === "breaks" && (
        <>
          {cluster && <div className="note">Showing breaks in cluster <strong>{String(cluster.signature)}</strong> <button className="link" onClick={() => setCluster(null)}>show all</button></div>}
          <Table rows={breaks} onRow={(r) => setEvidence({ type: "reconciliation_break", id: String(r.break_id) })} columns={[
            { key: "record_key", label: "record" }, { key: "entity" }, { key: "field" }, { key: "break_type", render: (r) => <Chip value={r.break_type} /> },
            { key: "expected_value", label: "expected" }, { key: "target_value", label: "loaded" }, { key: "delta" },
            { key: "tolerance", render: (r) => { const t = (r.body as Row)?.tolerance as Row | undefined;
              return t ? `${String(t.absolute_floor)} / ${String(t.relative_bp)}bp` : <span className="muted">exact</span>; } },
            { key: "date", render: (r) => fmt((r.body as Row)?.effective_date) },
            { key: "reason", render: (r) => fmt((r.body as Row)?.note) }]} />
          <Pager page={page} setPage={setPage} more={breaks.length === limit} />
        </>
      )}
      {tab === "customer outcomes" && (
        <>
          <div className="toolbar">
            <select value={band} onChange={(e) => { setBand(e.target.value); setPage(0); }}>
              <option value="">all materiality bands</option><option value="high">high</option><option value="medium">medium</option><option value="low">low</option>
            </select>
          </div>
          <Table rows={cases} onRow={setCaseRow} empty="No customer is materially affected." columns={[
            { key: "record_key", label: "customer record" }, { key: "impact_type", render: (r) => <Chip value={r.impact_type} /> },
            { key: "detriment" }, { key: "band", render: (r) => <Chip value={r.band} /> }, { key: "route" },
            { key: "vulnerable", render: (r) => ((r.body as Row)?.outcome as Row)?.vulnerable ? <span className="chip bad">vulnerable</span> : <span className="muted">-</span> },
            { key: "duty outcome", render: (r) => fmt((r.body as Row)?.duty_outcome) },
            { key: "note", render: (r) => ((r.body as Row)?.impact_note ? <span className="chip brand">impact note</span> : <span className="muted">-</span>) },
            { key: "status", render: (r) => <Chip value={r.status} /> }, { key: "decision" }]} />
          <Pager page={page} setPage={setPage} more={cases.length === limit} />
          {caseRow && <CaseDetail row={caseRow} onEvidence={() => setEvidence({ type: "conduct_case", id: String(caseRow.case_id) })} />}
        </>
      )}
      {tab === "controls" && runId && <Controls pid={pid} runId={runId} />}
      {evidence && <EvidenceDrawer pid={pid} entityType={evidence.type} entityId={evidence.id} onClose={() => setEvidence(null)} />}
    </section>
  );
}

function Controls({ pid, runId }: { pid: string; runId: string }) {
  const [s] = useLoad(() => api.waterfall(pid, runId), [pid, runId], null as Awaited<ReturnType<typeof api.waterfall>> | null);
  const c = (s?.controls ?? null) as Row | null;
  if (!c) return <div className="card empty">No derived values, cross-target checks or control totals configured for this project.</div>;
  const ctrl = (c.controls as Row[]) ?? [];
  return (
    <>
      <Table title="Control totals" rows={ctrl} empty="No control totals configured." columns={[
        { key: "name" }, { key: "regime", render: (r) => fmt(r.regime) },
        { key: "result", render: (r) => <Chip value={r.reconciled === true ? "reconciled" : r.reconciled === false ? "break" : "skipped"} /> },
        { key: "left_total", label: "left" }, { key: "right_total", label: "right" }, { key: "groups" },
        { key: "mismatches", render: (r) => ((r.mismatches as Row[]) ?? []).map((x) => `${String(x.group)}: ${String(x.difference)}`).join(", ") || <span className="muted">-</span> },
        { key: "skipped" }]} />
      <h3>Derived values</h3>
      <Table rows={(c.derived as Row[]) ?? []} empty="None configured." columns={[{ key: "name" }, { key: "compared" }, { key: "breaks" }, { key: "unpriced" }, { key: "skipped" }]} />
      <h3>Cross-target consistency</h3>
      <Table rows={(c.cross_target as Row[]) ?? []} empty="None configured." columns={[{ key: "name" }, { key: "compared" }, { key: "breaks" }, { key: "unmatched" }, { key: "skipped" }]} />
    </>
  );
}

function ShapeChip({ shape }: { shape?: Row }) {
  if (!shape || shape.kind === "exact") return <span className="muted">uniform</span>;
  if (shape.kind === "shift") return <span className="chip warn">systematic {(Number(shape.median_shift) * 100).toFixed(2)}%</span>;
  return <span className="chip">band: {String(shape.band)}</span>;
}

function CaseDetail({ row, onEvidence }: { row: Row; onEvidence: () => void }) {
  const body = (row.body ?? {}) as Row;
  const o = (body.outcome ?? {}) as Row;
  const note = body.impact_note as Row | undefined;
  const reasons = (o.vulnerability_reasons as string[]) ?? [];
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="toolbar"><h3 style={{ margin: 0 }}>Customer record {String(row.record_key)}</h3>
        <Chip value={row.impact_type} /><Chip value={row.band} />
        {o.vulnerable ? <span className="chip bad">vulnerable customer</span> : null}
        <span style={{ flex: 1 }} /><button className="secondary" onClick={onEvidence}>Evidence</button></div>
      <p className="small">Routed to <strong>{String(row.route)}</strong> · Consumer Duty outcome: <strong>{fmt(body.duty_outcome)}</strong>
        {" "}· annualised detriment {fmt(o.annualised_detriment)}</p>
      {reasons.length > 0 && <p className="small">Vulnerability indicators: {reasons.join(", ")}</p>}
      {Array.isArray(o.elements) && (o.elements as string[]).length > 0 && <p className="small">Elements affected: {(o.elements as string[]).join(", ")}</p>}
      <p>{String(o.rationale ?? "")}</p>
      {note && <Assisted title="Customer impact note" meta={note}><p>{String(note.impact)}</p>
        {Array.isArray(note.options) && <ul>{(note.options as string[]).map((x) => <li key={x}>{x}</li>)}</ul>}
        {note.question_for_the_human ? <p><strong>{String(note.question_for_the_human)}</strong></p> : null}</Assisted>}
    </div>
  );
}

function ClusterDetail({ row, onBreaks }: { row: Row; onBreaks: () => void }) {
  const body = (row.body ?? {}) as Row;
  const hyp = body.model_hypothesis as Row | undefined;
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="toolbar"><h3 style={{ margin: 0 }}>{String(row.signature)}</h3><Chip value={row.status} />
        <span style={{ flex: 1 }} /><button className="secondary" onClick={onBreaks}>View {String(row.count)} break(s)</button></div>
      <p>{String(body.hypothesis ?? "")}</p>
      {body.linked_rule ? <p className="small">Linked rule difference: <strong>{String(body.linked_rule)}</strong></p> : null}
      {hyp && <Assisted title="Unproven hypothesis" meta={hyp}><p>{String(hyp.hypothesis)}</p>
        {Array.isArray(hyp.checks) && <ul>{(hyp.checks as string[]).map((x) => <li key={x}>{x}</li>)}</ul>}</Assisted>}
    </div>
  );
}

function Pager({ page, setPage, more }: { page: number; setPage: (p: number) => void; more: boolean }) {
  return (
    <div className="toolbar" style={{ marginTop: 10 }}>
      <button className="secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
      <span className="muted small">page {page + 1}</span>
      <button className="secondary" disabled={!more} onClick={() => setPage(page + 1)}>Next</button>
    </div>
  );
}
