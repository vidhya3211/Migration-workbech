import { useEffect, useRef, useState, type DragEvent } from "react";
import type { PageProps } from "../App";
import { api, bytes, pollEvents, uploadBundle, type Overview, type Row, type Upload, type UploadProgress } from "../api/client";
import { Chip, ErrorNote, Kpi, PageHead, Progress, Table, fmt, useLoad } from "../components/ui";

// Project overview: upload the source / target bundles (chunked, 2-4 GB), start a run, follow it live.
export function OverviewPage({ pid, go }: PageProps) {
  const [ov, error, reload] = useLoad<Overview | null>(() => api.overview(pid), [pid], null);
  const run = ov?.latest_run ?? null;
  const active = run && ["created", "running", "queued"].includes(run.status);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(reload, 4000);
    return () => clearInterval(t);
  }, [active, pid]);   // eslint-disable-line react-hooks/exhaustive-deps

  const ready = (ov?.uploads ?? []).filter((u) => u.status === "ready");
  const latestFor = (role: string) => ready.find((u) => u.system_role === role);
  const src = latestFor("source");
  const tgt = latestFor("target");
  const [startError, setStartError] = useState<string | null>(null);
  const start = async () => {
    setStartError(null);
    try {
      await api.startRun(pid, [src, tgt].filter(Boolean).map((u) => (u as Upload).upload_id));
      reload();
    } catch (e) {
      setStartError((e as Error).message);
    }
  };
  const c = ov?.counts ?? {};
  return (
    <section>
      <PageHead title={ov?.project.name ?? "Project"} sub={ov ? `${ov.project.business_domain} · ${ov.project.source_technologies.join(", ") || "-"}${ov.project.target_technologies.length ? ` to ${ov.project.target_technologies.join(", ")}` : ""}` : ""}>
        {run && <Chip value={run.status} />}
      </PageHead>
      <ErrorNote error={error} />
      {ov?.project.description && <p className="muted">{ov.project.description}</p>}

      <div className="kpis">
        <Kpi label="Assets analysed" value={fmt(c.artefacts ?? 0)} sub={`${ov?.uploads.length ?? 0} upload(s)`} />
        <Kpi label="Business rules" value={fmt(c.rules ?? 0)} sub={`${c.certified_rules ?? 0} certified`} />
        <Kpi label="Rule differences" value={fmt(c.differentials ?? 0)} sub={`${c.mappings ?? 0} field mappings`} />
        <Kpi label="Pending reviews" value={fmt(c.pending_reviews ?? 0)} sub={run?.current_gate ? `gate: ${run.current_gate.replace(/_/g, " ")}` : "no open gate"}
          tone={c.pending_reviews ? "warn" : undefined} />
      </div>

      <div className="grid2">
        <UploadCard pid={pid} role="source" current={src} onDone={reload} />
        <UploadCard pid={pid} role="target" current={tgt} onDone={reload} />
      </div>

      <div className="card">
        <h3>Assessment run</h3>
        {run ? (
          <>
            <div className="toolbar">
              <Chip value={run.status} />
              <span className="muted">pipeline <strong>{run.pipeline.name}</strong> · {run.progress.steps_completed}/{run.progress.steps_total} steps</span>
              <div style={{ flex: 1, minWidth: 160 }}><Progress ratio={run.progress.ratio} /></div>
              {run.current_gate && <button onClick={() => go("reviews")}>Review {run.pending_tasks} item(s)</button>}
              {active && <button className="danger" onClick={() => api.halt(run.run_id).then(reload)}>Halt</button>}
            </div>
            {run.halted_reason && <div className="error">{run.halted_reason}</div>}
            {run.jobs.filter((j) => j.error).map((j) => <div key={j.job_id} className="error small">{j.kind}: {j.error}</div>)}
            <Timeline runId={run.run_id} live={!!active} />
          </>
        ) : <p className="muted">No run yet. Upload at least the source bundle, then start the assessment.</p>}
        <ErrorNote error={startError} />
        <div className="actions">
          <button onClick={start} disabled={!src || !!active}>{run ? "Start a new run" : "Start assessment"}</button>
        </div>
      </div>

      {run && <Waterfall pid={pid} runId={run.run_id} refreshKey={run.progress.steps_completed} />}

      <Table title="Uploads" rows={(ov?.uploads ?? []) as unknown as Row[]} columns={[
        { key: "system_role", label: "role" }, { key: "system_name", label: "system" }, { key: "filename" },
        { key: "size", render: (r) => bytes((r.bundle_bytes ?? r.declared_bytes) as number) },
        { key: "files", render: (r) => fmt(r.file_count) },
        { key: "status", render: (r) => <Chip value={r.status} /> }, { key: "error" }]} />
      <h3>Runs</h3>
      <Table rows={ov?.runs ?? []} columns={[{ key: "run_id" }, { key: "status", render: (r) => <Chip value={r.status} /> },
        { key: "pipeline_name", label: "pipeline" }, { key: "created_by" }, { key: "created_at" }, { key: "finished_at" }]} />
    </section>
  );
}

// Every source record accounted for: extracted = excluded + rejected + missing + loaded; loaded = reconciled +
// with breaks. An unbalanced row is itself a finding.
function Waterfall({ pid, runId, refreshKey }: { pid: string; runId: string; refreshKey: number }) {
  const [data] = useLoad(() => api.waterfall(pid, runId), [pid, runId, refreshKey], null as Awaited<ReturnType<typeof api.waterfall>> | null);
  const rows = data?.waterfall ?? [];
  if (!rows.length) return null;
  const stage = (label: string, n: unknown, total: number, tone = "") => (
    <div className="wf-stage" key={label}>
      <div className={`wf-bar ${tone}`} style={{ width: `${total ? Math.max(2, (Number(n) / total) * 100) : 0}%` }} />
      <span>{label}</span><strong>{fmt(n)}</strong>
    </div>
  );
  return (
    <div className="card">
      <h3>Record waterfall</h3>
      {rows.map((w) => {
        const total = Number(w.extracted) || 0;
        const reasons = { ...(w.excluded_reasons as Row), ...(w.rejected_reasons as Row) };
        return (
          <div key={String(w.pair)} className="wf">
            <div className="toolbar"><strong>{String(w.source_entity)} → {String(w.target_entity)}</strong>
              <Chip value={w.balanced ? "balanced" : "unbalanced"} />
              {w.date_aligned ? <span className="muted small">compared at the same date: {String(w.date_aligned)}</span> : null}</div>
            {stage("extracted", w.extracted, total)}
            {stage("excluded (out of scope)", w.excluded, total, "muted-bar")}
            {stage("rejected at load", w.rejected, total, "bad-bar")}
            {stage("missing from target", w.missing, total, "bad-bar")}
            {stage("loaded", w.loaded, total)}
            {stage("reconciled", w.reconciled, total, "good-bar")}
            {stage("loaded with breaks", w.with_breaks, total, "warn-bar")}
            {Number(w.unexpected_in_target) ? <div className="muted small">{String(w.unexpected_in_target)} target record(s) with no source record</div> : null}
            {Object.keys(reasons).length > 0 && <div className="muted small">Reasons: {Object.entries(reasons).map(([k, v]) => `${k} (${String(v)})`).join(" · ")}</div>}
            {Object.keys((w.excluded_but_loaded ?? {}) as Row).length > 0 &&
              <div className="note small">Loaded although out of scope: {Object.entries(w.excluded_but_loaded as Row).map(([k, v]) => `${k} (${String(v)})`).join(", ")}</div>}
          </div>
        );
      })}
    </div>
  );
}

function UploadCard({ pid, role, current, onDone }: { pid: string; role: "source" | "target"; current?: Upload; onDone: () => void }) {
  const [system, setSystem] = useState(current?.system_name ?? "");
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const abort = useRef<AbortController | null>(null);
  useEffect(() => { if (current && !system) setSystem(current.system_name); }, [current]);   // eslint-disable-line react-hooks/exhaustive-deps
  const send = async () => {
    if (!file || !system.trim()) return;
    setError(null);
    abort.current = new AbortController();
    try {
      const up = await uploadBundle(pid, file, system.trim(), role, setProgress, abort.current.signal);
      if (up.status !== "ready") setError(up.error ?? `upload ${up.status}`);
      setFile(null);
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      abort.current = null;
    }
  };
  const drop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) setFile(f);
  };
  const busy = progress && (progress.phase === "uploading" || progress.phase === "validating");
  return (
    <div className="card">
      <h3>{role === "source" ? "Source system (legacy)" : "Target system"}</h3>
      {current && <div className="note">Current: <strong>{current.filename}</strong> · {bytes(current.bundle_bytes)} · {fmt(current.file_count)} files</div>}
      <label className="field">System name
        <input placeholder={role === "source" ? "e.g. Legacy policy admin" : "e.g. Target platform"} value={system} onChange={(e) => setSystem(e.target.value)} />
      </label>
      <div className={`dropzone ${over ? "over" : ""}`} onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)} onDrop={drop}>
        {file ? <div><strong>{file.name}</strong> · {bytes(file.size)}</div> : <div>Drop a .zip bundle here (up to the configured limit), or</div>}
        <input type="file" accept=".zip" style={{ marginTop: 8 }} onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
      </div>
      {progress && (
        <div style={{ marginTop: 10 }}>
          <Progress ratio={progress.totalBytes ? progress.sentBytes / progress.totalBytes : 0} />
          <div className="muted small">{progress.phase === "uploading" ? `${bytes(progress.sentBytes)} of ${bytes(progress.totalBytes)}`
            : progress.phase === "validating" ? "Validating bundle (manifest, safety checks, PII protection)..." : progress.phase}</div>
        </div>
      )}
      <ErrorNote error={error} />
      <div className="actions">
        {busy && <button className="secondary" onClick={() => abort.current?.abort()}>Cancel</button>}
        <button onClick={send} disabled={!file || !system.trim() || !!busy}>Upload</button>
      </div>
    </div>
  );
}

function Timeline({ runId, live }: { runId: string; live: boolean }) {
  const [events, setEvents] = useState<Row[]>([]);
  useEffect(() => {
    let last = 0;
    let stop = false;
    setEvents([]);
    const tick = async () => {
      try {
        const r = await pollEvents(runId, last);
        last = r.last;
        if (r.events.length) setEvents((e) => [...e, ...r.events].slice(-500));
      } catch { /* retry on the next tick */ }
      if (!stop && live) setTimeout(tick, 2000);
    };
    tick();
    return () => { stop = true; };
  }, [runId, live]);
  return (
    <details open={live}>
      <summary className="muted">Activity ({events.length} events)</summary>
      <div className="timeline">
        {[...events].reverse().map((e) => (
          <div key={String(e.seq)} className={`event level-${String(e.level)}`}>
            <span className="muted small">{String(e.created_at).slice(11, 19)}</span>
            <span className="agent">{String(e.agent).replace(/_/g, " ")}</span>
            <span>{String(e.message)}</span>
          </div>
        ))}
      </div>
    </details>
  );
}
