import { useState } from "react";
import type { PageProps } from "../App";
import { api, type Row } from "../api/client";
import { EvidenceDrawer } from "../components/EvidenceDrawer";
import { NoRun, RunPicker, useRuns } from "../components/RunPicker";
import { Assisted, Chip, ErrorNote, PageHead, Table, Tabs, fmt, useLoad } from "../components/ui";

// What the agents found: business rules (with evidence), rule differences between systems, and field mappings.
const TABS = ["rule differences", "business rules", "field mappings", "data quality", "customer matches", "coverage"] as const;

export function AssessmentPage({ pid }: PageProps) {
  const [runs, runId, setRunId] = useRuns(pid);
  const [tab, setTab] = useState<(typeof TABS)[number]>("rule differences");
  const [page, setPage] = useState(0);
  const [evidence, setEvidence] = useState<{ type: string; id: string } | null>(null);
  const [selected, setSelected] = useState<Row | null>(null);
  const limit = 100;
  const kind = { "rule differences": "differentials", "business rules": "rules", "field mappings": "mappings",
    "data quality": "dq/cohorts", "customer matches": "dq/matches", coverage: "" }[tab];
  const [rows, error] = useLoad<Row[]>(() => (runId && kind ? api.list(pid, kind, { run_id: runId, limit, offset: page * limit }) : Promise.resolve([])),
    [pid, runId, kind, page], []);
  if (!runs.length) return <section><PageHead title="Assessment" /><NoRun /></section>;
  const ev = (type: string, key: string) => (r: Row) => setEvidence({ type, id: String(r[key]) });
  return (
    <section>
      <PageHead title="Assessment" sub="Business rules, rule differences and field mappings - each traceable to its evidence">
        <RunPicker runs={runs} runId={runId} onChange={(id) => { setRunId(id); setPage(0); }} />
      </PageHead>
      <Tabs tabs={TABS} value={tab} onChange={(t) => { setTab(t); setPage(0); setSelected(null); }} />
      <ErrorNote error={error} />
      {tab === "rule differences" && (
        <>
          <Table rows={rows} onRow={setSelected} empty="No differences between the systems' rules." columns={[
            { key: "canonical_name", label: "rule" }, { key: "difference_type", render: (r) => <Chip value={r.difference_type} /> },
            { key: "severity_score", label: "severity" }, { key: "financial_exposure", label: "exposure" },
            { key: "real book", render: (r) => { const b = (r.body as Row)?.real_book as Row | undefined;
              return b ? `${String(b.affected)} of ${String(b.records_evaluated)}` : <span className="muted">-</span>; } },
            { key: "status", render: (r) => <Chip value={r.status} /> },
            { key: "brief", render: (r) => ((r.brief as Row)?.narrative ? <span className="chip brand">model brief</span> : <span className="muted">-</span>) }]} />
          {selected && <DifferentialDetail row={selected} onEvidence={() => ev("differential", "differential_id")(selected)} />}
        </>
      )}
      {tab === "business rules" && (
        <Table rows={rows} onRow={ev("rule", "rule_id")} columns={[
          { key: "canonical_name", label: "rule" }, { key: "kind" }, { key: "expression" }, { key: "conditions" },
          { key: "system_name", label: "system" }, { key: "agent" }, { key: "confidence" },
          { key: "status", render: (r) => <Chip value={r.status} /> }]} />
      )}
      {tab === "field mappings" && (
        <Table rows={rows} onRow={ev("mapping", "mapping_id")} columns={[
          { key: "source", render: (r) => `${String(r.source_entity)}.${String(r.source_field)}` },
          { key: "target", render: (r) => `${String(r.target_entity)}.${String(r.target_field)}` },
          { key: "score" }, { key: "transformation", render: (r) => fmt((r.transformation as Row)?.description) },
          { key: "validation", render: (r) => <Chip value={(r.validation as Row)?.verdict} /> }, { key: "reasoning_mode", label: "reasoning" },
          { key: "status", render: (r) => <Chip value={r.status} /> }]} />
      )}
      {tab === "coverage" && runId && <Coverage pid={pid} runId={runId} />}
      {tab === "data quality" && selected && <CohortDetail row={selected} onEvidence={() => ev("cohort", "cohort_id")(selected)} />}
      {tab === "customer matches" && (
        <Table rows={rows} onRow={ev("entity_match", "match_id")} empty="No probable same-customer records." columns={[
          { key: "left_ref", label: "record" }, { key: "right_ref", label: "same customer as" },
          { key: "score", render: (r) => fmt(r.score) },
          { key: "grade", render: (r) => <Chip value={(r.body as Row)?.grade} /> },
          { key: "blocked on", render: (r) => fmt((r.body as Row)?.blocked_on) },
          { key: "conflicts", render: (r) => { const c = ((r.body as Row)?.conflicts as Row[]) ?? [];
            return c.length ? <span className="chip bad">{c.map((x) => String(x.field).toLowerCase()).join(", ")}</span> : <span className="muted">-</span>; } },
          { key: "status", render: (r) => <Chip value={r.status} /> }]} />
      )}
      {tab === "data quality" && (
        <Table rows={rows} onRow={setSelected} empty="No data-quality defects found." columns={[
          { key: "entity" }, { key: "column" }, { key: "defect_code", render: (r) => <Chip value={r.defect_code} /> },
          { key: "record_count", label: "records" }, { key: "root cause", render: (r) => fmt((r.root_cause as Row)?.cause_code) },
          { key: "status", render: (r) => <Chip value={r.status} /> }]} />
      )}
      {tab !== "coverage" && <div className="toolbar" style={{ marginTop: 10 }}>
        <button className="secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
        <span className="muted small">page {page + 1}</span>
        <button className="secondary" disabled={rows.length < limit} onClick={() => setPage(page + 1)}>Next</button>
      </div>}
      {evidence && <EvidenceDrawer pid={pid} entityType={evidence.type} entityId={evidence.id} onClose={() => setEvidence(null)} />}
    </section>
  );
}

const STATE_TONE: Record<string, string> = { certified: "good", differs: "bad", source_only: "bad", target_only: "warn",
  partly_certified: "warn", candidate: "brand", none: "" };

function Coverage({ pid, runId }: { pid: string; runId: string }) {
  const [cov, error] = useLoad(() => api.coverage(pid, runId), [pid, runId], null as Awaited<ReturnType<typeof api.coverage>> | null);
  if (error) return <ErrorNote error={error} />;
  if (!cov) return null;
  return (
    <div className="table-wrap">
      <div className="table-title">Rule coverage by product {cov.product_field ? <span className="muted small">({cov.product_field})</span>
        : <span className="muted small">- set heuristics.coverage.product_field to split by product</span>}</div>
      <table>
        <thead><tr><th>domain</th>{cov.products.map((p) => <th key={p.product}>{p.product}{p.records ? <div className="muted small">{p.records} records</div> : null}</th>)}</tr></thead>
        <tbody>{cov.matrix.map((row) => (
          <tr key={row.domain}><td>{row.domain.replace(/_/g, " ")}</td>
            {cov.products.map((p) => { const c = row.cells[p.product];
              return <td key={p.product}><span className={`chip ${STATE_TONE[c.state] ?? ""}`}>{c.state.replace(/_/g, " ")}</span>
                {c.rules ? <span className="muted small"> {c.certified}/{c.rules}</span> : null}</td>; })}
          </tr>))}</tbody>
      </table>
    </div>
  );
}

function CohortDetail({ row, onEvidence }: { row: Row; onEvidence: () => void }) {
  const body = (row.body ?? {}) as Row;
  const facts = (((body.anomaly ?? {}) as Row).evidence as string[]) ?? [];
  const rc = (row.root_cause ?? {}) as Row;
  const cl = (row.cleansing ?? {}) as Row;
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="toolbar"><h3 style={{ margin: 0 }}>{String(row.entity)}.{String(row.column ?? "")}</h3>
        <Chip value={row.defect_code} /><span className="muted">{fmt(row.record_count)} record(s)</span>
        <span style={{ flex: 1 }} /><button className="secondary" onClick={onEvidence}>Evidence</button></div>
      <p>{String(body.description ?? "")}</p>
      {facts.length > 0 && <><h4>What was measured</h4><ul className="facts">{facts.map((f) => <li key={f}>{f}</li>)}</ul></>}
      {rc.cause_code ? <p className="small"><strong>Probable cause:</strong> {String(rc.cause_code)} - {String(rc.description ?? "")}
        {rc.score !== undefined ? ` (confidence ${Number(rc.score).toFixed(2)})` : ""}</p> : null}
      {cl.action ? <p className="small"><strong>Proposed treatment:</strong> {String(cl.action).replace(/_/g, " ")} - {String(cl.rationale ?? "")}</p> : null}
      {Array.isArray(body.record_refs) && (body.record_refs as string[]).length > 0 &&
        <p className="muted small">Sample records: {(body.record_refs as string[]).slice(0, 10).join(", ")}</p>}
    </div>
  );
}

function DifferentialDetail({ row, onEvidence }: { row: Row; onEvidence: () => void }) {
  const body = (row.body ?? {}) as Row;
  const brief = (row.brief ?? {}) as Row;
  const n = brief.narrative as Row | undefined;
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="toolbar"><h3 style={{ margin: 0 }}>{String(row.canonical_name)}</h3><Chip value={row.difference_type} />
        <span style={{ flex: 1 }} /><button className="secondary" onClick={onEvidence}>Evidence</button></div>
      <p>{String(body.explanation ?? "")}</p>
      <div className="grid2">
        <div><h4>Source</h4><code>{fmt(body.source_summary)}</code></div>
        <div><h4>Target</h4><code>{fmt(body.target_summary)}</code></div>
      </div>
      {body.document_summary ? <p className="small"><strong>Documentation:</strong> {String(body.document_summary)}</p> : null}
      {body.real_book ? (() => { const b = body.real_book as Row; return (
        <div className="note small"><strong>On the real book:</strong> {String(b.affected)} of {String(b.records_evaluated)} records in {String(b.extract)} get a different result
          {b.delta_median ? ` - median difference ${String(b.delta_median)}, largest ${String(b.delta_max)}, total ${String(b.delta_total)}` : ""}
          {b.sampled ? " (sampled)" : ""}.</div>); })() : null}
      {Array.isArray(body.divergence) && body.divergence.length > 0 && (
        <Table title="Proven divergence (test vectors)" rows={body.divergence as Row[]} columns={[
          { key: "inputs" }, { key: "source_value", label: "source gives" }, { key: "target_value", label: "target gives" }]} />
      )}
      {n && (
        <Assisted title="Decision brief" meta={brief.model_assisted as Row}>
          <p>{String(n.summary ?? "")}</p>
          {n.impact ? <p><strong>Impact:</strong> {String(n.impact)}</p> : null}
          {Array.isArray(n.questions) && <ul>{(n.questions as string[]).map((x) => <li key={x}>{x}</li>)}</ul>}
        </Assisted>
      )}
    </div>
  );
}
