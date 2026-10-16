import { useEffect, useState } from "react";
import { api, type Row } from "../api/client";

/** The run a project page shows: the latest by default, or one the administrator picks. */
export function useRuns(pid: string): [Row[], string | null, (id: string) => void] {
  const [runs, setRuns] = useState<Row[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  useEffect(() => {
    setRunId(null);
    api.runs(pid).then((rs) => { setRuns(rs); setRunId(rs.length ? String(rs[0].run_id) : null); }).catch(() => setRuns([]));
  }, [pid]);
  return [runs, runId, setRunId];
}

export function RunPicker({ runs, runId, onChange }: { runs: Row[]; runId: string | null; onChange: (id: string) => void }) {
  if (runs.length < 2) return runId ? <span className="muted small">run {runId}</span> : null;
  return (
    <select style={{ width: "auto" }} value={runId ?? ""} onChange={(e) => onChange(e.target.value)} aria-label="Run">
      {runs.map((r) => <option key={String(r.run_id)} value={String(r.run_id)}>{String(r.run_id)} · {String(r.status)}</option>)}
    </select>
  );
}

export function NoRun() {
  return <div className="card empty">No assessment run yet - upload the bundles and start one from Overview.</div>;
}
