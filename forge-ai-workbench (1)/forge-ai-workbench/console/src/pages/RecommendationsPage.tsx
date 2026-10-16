import { useState } from "react";
import type { PageProps } from "../App";
import { api, type Row } from "../api/client";
import { NoRun, RunPicker, useRuns } from "../components/RunPicker";
import { Assisted, Chip, ErrorNote, PageHead, Table, Tabs, useLoad } from "../components/ui";

// What the workbench recommends: cleansing actions, build stories for certified rules and generated
// transformation code. All are proposals - a named human approves them in Reviews.
const TABS = ["cleansing", "user stories", "transformation code"] as const;

export function RecommendationsPage({ pid }: PageProps) {
  const [runs, runId, setRunId] = useRuns(pid);
  const [tab, setTab] = useState<(typeof TABS)[number]>("cleansing");
  const [open, setOpen] = useState<Row | null>(null);
  const kind = { cleansing: "dq/cohorts", "user stories": "stories", "transformation code": "generated-code" }[tab];
  const [rows, error] = useLoad<Row[]>(() => (runId ? api.list(pid, kind, { run_id: runId, limit: 500 }) : Promise.resolve([])), [pid, runId, kind], []);
  if (!runs.length) return <section><PageHead title="Recommendations" /><NoRun /></section>;
  return (
    <section>
      <PageHead title="Recommendations" sub="Proposals for the people who build and cleanse - each one approved by a named person">
        <RunPicker runs={runs} runId={runId} onChange={setRunId} />
      </PageHead>
      <Tabs tabs={TABS} value={tab} onChange={(t) => { setTab(t); setOpen(null); }} />
      <ErrorNote error={error} />
      {tab === "cleansing" && (
        <Table rows={rows} onRow={setOpen} empty="No cleansing needed." columns={[
          { key: "defect", render: (r) => `${String(r.entity)}.${String(r.column)}: ${String(r.defect_code).replace(/_/g, " ")}` },
          { key: "record_count", label: "records" },
          { key: "action", render: (r) => <Chip value={(r.cleansing as Row)?.action} /> },
          { key: "rationale", render: (r) => String((r.cleansing as Row)?.rationale ?? "") },
          { key: "source", render: (r) => ((r.cleansing as Row)?.model_assisted ? <span className="chip brand">model proposed</span> : "playbook") },
          { key: "status", render: (r) => <Chip value={r.status} /> }]} />
      )}
      {tab === "user stories" && (
        <Table rows={rows} onRow={setOpen} empty="Stories are written once rules are certified." columns={[
          { key: "title" }, { key: "acceptance_criteria", label: "criteria", render: (r) => ((r.acceptance_criteria as unknown[]) ?? []).length },
          { key: "source", render: (r) => (r.model_assisted ? <span className="chip brand">model written</span> : "template") },
          { key: "status", render: (r) => <Chip value={r.status} /> }]} />
      )}
      {tab === "transformation code" && (
        <Table rows={rows} onRow={setOpen} empty="Code is generated after mappings are approved." columns={[
          { key: "module_name", label: "module" }, { key: "language" }, { key: "mapping_ids", label: "mappings", render: (r) => ((r.mapping_ids as unknown[]) ?? []).length },
          { key: "validation", render: (r) => <Chip value={Object.values((r.validation ?? {}) as Row).every(Boolean) ? "pass" : "review"} /> },
          { key: "status", render: (r) => <Chip value={r.status} /> }]} />
      )}
      {open && tab === "user stories" && (
        <div className="card" style={{ marginTop: 14 }}>
          <h3>{String(open.title)}</h3>
          {open.model_assisted ? <Assisted title="Story" meta={open.model_assisted as Row}><p>{String(open.narrative)}</p></Assisted> : <p>{String(open.narrative)}</p>}
          <h4>Acceptance criteria</h4>
          <ul>{((open.acceptance_criteria as string[]) ?? []).map((c, i) => <li key={i}>{c}</li>)}</ul>
        </div>
      )}
      {open && tab === "transformation code" && (
        <div className="card" style={{ marginTop: 14 }}>
          <h3>{String(open.module_name)}</h3>
          <pre className="json">{String(open.source)}</pre>
          <h4>Tests</h4>
          <pre className="json">{String(open.tests)}</pre>
        </div>
      )}
      {open && tab === "cleansing" && (open.cleansing as Row)?.model_assisted ? (
        <div className="card" style={{ marginTop: 14 }}>
          <Assisted title="Cleansing proposal" meta={(open.cleansing as Row).model_assisted as Row}>
            <p><strong>{String((open.cleansing as Row).action)}</strong> - {String((open.cleansing as Row).rationale)}</p>
          </Assisted>
        </div>
      ) : null}
    </section>
  );
}
