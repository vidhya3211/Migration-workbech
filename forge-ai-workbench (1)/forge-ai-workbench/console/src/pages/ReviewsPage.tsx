import { useState } from "react";
import type { PageProps } from "../App";
import { api, type Row, type RunStatus } from "../api/client";
import { EvidenceDrawer } from "../components/EvidenceDrawer";
import { NoRun, RunPicker, useRuns } from "../components/RunPicker";
import { Assisted, Chip, ErrorNote, Json, PageHead, Tabs, useLoad } from "../components/ui";

// Human gates: every open item at the current gate, with its evidence brief. Decisions are attributed to the
// signed-in administrator and recorded in the audit trail; the run resumes when the gate's items are decided.
const DECISIONS: Record<string, string[]> = {
  differential: ["accept_source", "accept_target", "override", "reject"],
  rule: ["approve", "reject"],
  mapping: ["approve", "reject"],
  entity_match: ["approve", "reject"],
  cohort: ["approve", "reject"],
  break_cluster: ["accept_variance", "escalate", "reject"],
  conduct_case: ["remediate", "no_action", "reject"],
  go_no_go: ["go", "no_go"],
};
const EVIDENCE_TYPE: Record<string, string> = {
  differential: "differential", mapping: "mapping", entity_match: "entity_match", cohort: "cohort",
  conduct_case: "conduct_case",
};
const VIEWS = ["open", "decided"] as const;

export function ReviewsPage({ pid }: PageProps) {
  const [runs, runId, setRunId] = useRuns(pid);
  const [status, , reloadStatus] = useLoad<RunStatus | null>(() => (runId ? api.run(runId) : Promise.resolve(null)), [runId], null);
  const [tasks, error, reload] = useLoad<Row[]>(() => (runId ? api.list(pid, "hitl/tasks", { run_id: runId, limit: 500 }) : Promise.resolve([])),
    [pid, runId], []);
  const [view, setView] = useState<(typeof VIEWS)[number]>("open");
  const [evidence, setEvidence] = useState<{ type: string; id: string } | null>(null);
  const [gateError, setGateError] = useState<string | null>(null);
  if (!runs.length) return <section><PageHead title="Reviews" /><NoRun /></section>;
  const gate = status?.current_gate ?? null;
  const open = tasks.filter((t) => t.status === "pending");
  const shown = view === "open" ? open : tasks.filter((t) => t.status !== "pending");
  const refresh = () => { reload(); reloadStatus(); };
  const closeGate = async () => {
    setGateError(null);
    try { await api.resume(String(runId)); refresh(); } catch (e) { setGateError((e as Error).message); }
  };
  return (
    <section>
      <PageHead title="Reviews" sub={gate ? `The run is waiting at the ${gate.replace(/_/g, " ")} gate` : "No gate is open for this run"}>
        <RunPicker runs={runs} runId={runId} onChange={setRunId} />
      </PageHead>
      <ErrorNote error={error ?? gateError} />
      <div className="toolbar">
        <Tabs tabs={VIEWS} value={view} onChange={setView} />
        <span className="muted">{open.length} open · {tasks.length - open.length} decided</span>
        <span style={{ flex: 1 }} />
        {gate && <button className="secondary" onClick={closeGate} title="Undecided items become unresolved - never approved">Close gate and continue</button>}
      </div>
      {shown.length === 0 && <div className="card empty">{view === "open" ? "Nothing waiting for a decision." : "No decisions yet."}</div>}
      {shown.map((t) => (
        <TaskCard key={String(t.task_id)} pid={pid} task={t} onDone={refresh}
          onEvidence={EVIDENCE_TYPE[String(t.item_type)] ? () => setEvidence({ type: EVIDENCE_TYPE[String(t.item_type)], id: String(t.item_id) }) : undefined} />
      ))}
      {evidence && <EvidenceDrawer pid={pid} entityType={evidence.type} entityId={evidence.id} onClose={() => setEvidence(null)} />}
    </section>
  );
}

function TaskCard({ pid, task, onDone, onEvidence }: { pid: string; task: Row; onDone: () => void; onEvidence?: () => void }) {
  const [rationale, setRationale] = useState("");
  const [expression, setExpression] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const type = String(task.item_type);
  const brief = (task.brief ?? {}) as Row;
  const adj = (brief.brief ?? {}) as Row;
  const narrative = adj.narrative as Row | undefined;
  const hyp = brief.model_hypothesis as Row | undefined;
  const note = brief.impact_note as Row | undefined;
  const cleansing = (brief.cleansing ?? {}) as Row;
  const decide = async (decision: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.decideTask(pid, String(task.task_id), { decision, rationale,
        certified_expression: decision === "override" ? expression : null });
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="card task">
      <header>
        <strong>{String(task.title)}</strong>
        <Chip value={task.status} />
        <span className="muted small">{String(task.gate).replace(/_/g, " ")} · decided as {String(task.required_role).replace(/_/g, " ")}</span>
        {onEvidence && <button className="link" onClick={onEvidence}>evidence</button>}
      </header>
      {typeof brief.explanation === "string" && <p>{brief.explanation}</p>}
      {typeof brief.description === "string" && <p>{brief.description}</p>}
      {typeof brief.hypothesis === "string" && <p>{brief.hypothesis}</p>}
      {adj.question ? <p><strong>{String(adj.question)}</strong> <span className="muted small">{String(adj.exposure_note ?? "")}</span></p> : null}
      {narrative && (
        <Assisted title="Decision brief" meta={adj.model_assisted as Row}>
          <p>{String(narrative.summary ?? "")}</p>
          {narrative.source_reading ? <p><strong>Source:</strong> {String(narrative.source_reading)}</p> : null}
          {narrative.target_reading ? <p><strong>Target:</strong> {String(narrative.target_reading)}</p> : null}
          {narrative.impact ? <p><strong>Impact:</strong> {String(narrative.impact)}</p> : null}
          {Array.isArray(narrative.questions) && narrative.questions.length > 0 && <ul>{(narrative.questions as string[]).map((x) => <li key={x}>{x}</li>)}</ul>}
        </Assisted>
      )}
      {cleansing.action ? <p className="small">Proposed cleansing: <strong>{String(cleansing.action)}</strong> - {String(cleansing.rationale ?? "")}</p> : null}
      {hyp && <Assisted title="Unproven hypothesis" meta={hyp}><p>{String(hyp.hypothesis)}</p>
        {Array.isArray(hyp.checks) && <ul>{(hyp.checks as string[]).map((x) => <li key={x}>{x}</li>)}</ul>}</Assisted>}
      {note && <Assisted title="Customer impact note" meta={note}><p>{String(note.impact)}</p>
        {Array.isArray(note.options) && <ul>{(note.options as string[]).map((x) => <li key={x}>{x}</li>)}</ul>}
        {note.question_for_the_human ? <p><strong>{String(note.question_for_the_human)}</strong></p> : null}</Assisted>}
      {type === "rule" && Array.isArray(brief.rule_ids) && (
        <p className="muted">{(brief.rule_ids as unknown[]).length} rule(s) proven equivalent or uncontested - one decision certifies them all.</p>
      )}
      <details><summary className="muted small">full brief</summary><Json value={brief} /></details>
      {task.status === "pending" ? (
        <div className="decide">
          <input placeholder="Rationale (recorded in the audit trail)" value={rationale} onChange={(e) => setRationale(e.target.value)} />
          {type === "differential" && <input placeholder="Certified expression (required for override)" value={expression} onChange={(e) => setExpression(e.target.value)} />}
          {(DECISIONS[type] ?? ["approve", "reject"]).map((d) => (
            <button key={d} disabled={busy} className={/reject|no_go|escalate/.test(d) ? "secondary" : ""} onClick={() => decide(d)}>{d.replace(/_/g, " ")}</button>
          ))}
        </div>
      ) : (
        <div className="muted small">Decision: <strong>{String(task.decision ?? "-")}</strong> by {String(task.decided_by ?? "-")} {task.decided_at ? `at ${String(task.decided_at)}` : ""}</div>
      )}
      <ErrorNote error={error} />
    </div>
  );
}
