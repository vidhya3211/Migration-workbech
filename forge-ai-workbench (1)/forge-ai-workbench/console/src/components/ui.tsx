import { Component, useEffect, useState, type ErrorInfo, type ReactNode } from "react";
import type { Row } from "../api/client";

export function Chip({ value }: { value: unknown }) {
  const v = String(value ?? "-").replace(/_/g, " ");
  const tone =
    /certified|completed|complete|agree|^ok|pass|^go$|approved|ready|accept|remediate/.test(v) ? "good"
    : /reject|fail|error|blocked|halt|disagree|refusal|no go|high|critical/.test(v) ? "bad"
    : /pending|awaiting|proposed|concerns|warn|unresolved|medium|review|validating|uploading/.test(v) ? "warn"
    : /assessment|running|upload|created/.test(v) ? "brand"
    : "";
  return <span className={`chip ${tone}`}>{v}</span>;
}

export function Progress({ ratio }: { ratio: number }) {
  const pct = Math.round(Math.max(0, Math.min(1, ratio)) * 100);
  return (
    <div className="progress-cell">
      <div className="bar"><span style={{ width: `${pct}%` }} /></div>
      <span className="pct">{pct}%</span>
    </div>
  );
}

export interface Column {
  key: string;
  label?: string;
  render?: (row: Row) => ReactNode;
}

export function Table({ rows, columns, onRow, empty = "Nothing here yet.", title }: {
  rows: Row[]; columns: Column[]; onRow?: (row: Row) => void; empty?: string; title?: ReactNode;
}) {
  return (
    <div className="table-wrap">
      {title && <div className="table-title">{title}</div>}
      {!rows.length ? <div className="empty">{empty}</div> : (
        <table>
          <thead>
            <tr>{columns.map((c) => <th key={c.key}>{c.label ?? c.key.replace(/_/g, " ")}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} onClick={onRow ? () => onRow(r) : undefined} className={onRow ? "clickable" : undefined}>
                {columns.map((c) => <td key={c.key}>{c.render ? c.render(r) : fmt(r[c.key])}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function fmt(v: unknown): ReactNode {
  if (v === null || v === undefined || v === "") return <span className="muted">-</span>;
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toFixed(3);
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) return v.map(String).join(", ");
  if (typeof v === "object") return <code className="inline">{JSON.stringify(v).slice(0, 120)}</code>;
  const s = String(v);
  return s.length > 180 ? `${s.slice(0, 177)}...` : s;
}

export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

export function Kpi({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: "good" | "bad" | "warn" }) {
  return (
    <div className={`kpi ${tone ?? ""}`}>
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub !== undefined && <div className="sub">{sub}</div>}
    </div>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  return error ? <div className="error">{error}</div> : null;
}

export function PageHead({ title, sub, children }: { title: string; sub?: ReactNode; children?: ReactNode }) {
  return (
    <div className="page-head">
      <div className="grow">
        <h1>{title}</h1>
        {sub && <div className="muted">{sub}</div>}
      </div>
      {children}
    </div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: readonly T[]; value: T; onChange: (t: T) => void }) {
  return (
    <nav className="tabs">
      {tabs.map((t) => <button key={t} className={t === value ? "active" : ""} onClick={() => onChange(t)}>{t}</button>)}
    </nav>
  );
}

// Model-drafted text is always labelled as advisory, with the model and prompt version that produced it.
export function Assisted({ title, meta, children }: { title: string; meta?: Row | null; children: ReactNode }) {
  return (
    <div className="assist">
      <div className="tag">{title} - model drafted, advisory</div>
      {children}
      {meta && <div className="muted small">{String(meta.model ?? "")} · prompt {String(meta.prompt_version ?? "")} · {String(meta.guard ?? "")}</div>}
    </div>
  );
}

/** Load a value and reload it when deps change; `reload()` re-fetches on demand. */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[], initial: T): [T, string | null, () => void, boolean] {
  const [value, setValue] = useState<T>(initial);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    setLoading(true);
    setValue(initial);          // never render the previous query's rows with this query's columns
    fn().then((v) => { if (live) { setValue(v); setError(null); } })
      .catch((e: Error) => { if (live) setError(e.message); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return [value, error, () => setTick((t) => t + 1), loading];
}

export const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);

/** A page that fails to render shows what failed instead of blanking the whole console. */
export class PageBoundary extends Component<{ children: ReactNode; resetKey: string }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(e: Error) { return { error: e.message }; }
  componentDidCatch(e: Error, info: ErrorInfo) { console.error("page failed", e, info.componentStack); }
  componentDidUpdate(prev: { resetKey: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }
  render() {
    return this.state.error
      ? <div className="error">This view could not be shown: {this.state.error}. Try another page or reload.</div>
      : this.props.children;
  }
}
