// API client for the AI Workbench console. Every call goes to /v1 with the administrator's bearer token.
// Route paths are checked against the generated OpenAPI contract (schema.d.ts, `npm run gen:api`).
import type { paths } from "./schema";

export type Row = Record<string, unknown>;
type ApiPath = keyof paths;

export interface Meta {
  organisation_name: string;
  product_name: string;
  business_domains: string[];
  pipelines: string[];
  default_pipeline: string;
  human_roles: Record<string, string>;
  upload_chunk_bytes: number;
  max_upload_bytes: number;
}

export interface PortfolioRow {
  project_id: string;
  name: string;
  business_domain: string;
  source_technologies: string[];
  target_technologies: string[];
  stage: string;
  progress: number;
  readiness: number | null;
  risks: number;
  run_id: string | null;
  run_status: string | null;
  pending_approvals: number;
}

export interface Portfolio {
  kpis: {
    active_projects: number; in_assessment: number; average_readiness: number | null; assessed: number;
    critical_risks: number; high_severity: number; pending_approvals: number; low_confidence: number;
  };
  projects: PortfolioRow[];
}

export interface Project {
  project_id: string;
  name: string;
  business_domain: string;
  source_technologies: string[];
  target_technologies: string[];
  description: string;
  status: string;
  created_at: string;
}

export interface Upload {
  upload_id: string;
  project_id: string;
  system_name: string;
  system_role: string;
  filename: string;
  status: string;
  declared_bytes: number | null;
  chunk_bytes: number | null;
  chunks_expected: number | null;
  bundle_bytes: number | null;
  uncompressed_bytes: number | null;
  file_count: number | null;
  error: string | null;
  received_at: string;
}

export interface RunStatus {
  run_id: string;
  project_id: string;
  status: string;
  current_gate: string | null;
  source_system: string | null;
  target_system: string | null;
  created_by: string;
  created_at: string;
  finished_at: string | null;
  halted_reason: string | null;
  summary: Row | null;
  ledger: Row | null;
  pending_tasks: number;
  pipeline: { name: string; hash: string; phases: unknown[] };
  progress: { steps_total: number; steps_completed: number; ratio: number; completed: string[] };
  jobs: { job_id: string; kind: string; status: string; attempts: number; error: string }[];
}

export interface Overview {
  project: Project;
  latest_run: RunStatus | null;
  counts: Record<string, number>;
  runs: Row[];
  uploads: Upload[];
}

export interface Readiness {
  run_id: string;
  readiness_score: number;
  assurance_passed: boolean;
  blockers: string[];
  capabilities: Row[];
  gates: Row;
  go_no_go: Row;
}

export interface Decision {
  decision: string;
  rationale?: string;
  certified_expression?: string | null;
}

export interface Answer {
  answer: string;
  citations: Row[] | string[];
  refused: boolean;
  refusal_reason?: string | null;
  run_id: string;
}

export interface Graph {
  run_id: string;
  nodes: { id: string; kind: string; label: string; system_role: string | null }[];
  edges: { from: string; to: string; kind: string; count: number }[];
  truncated: boolean;
  total_edges: number;
}

// ------------------------------------------------------------------ session (administrator sign-in)
const TOKEN_KEY = "aiwb.session";

export interface Session { token: string; user: string; expiresAt: string }

export function getSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(TOKEN_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Session;
    return new Date(s.expiresAt).getTime() > Date.now() ? s : null;
  } catch {
    return null;
  }
}

function saveSession(s: Session | null): void {
  try {
    if (s) sessionStorage.setItem(TOKEN_KEY, JSON.stringify(s));
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: the session lasts for this page only */
  }
}

let onSignedOut: () => void = () => undefined;
export function whenSignedOut(fn: () => void): void { onSignedOut = fn; }

export function signOut(): void {
  saveSession(null);
  onSignedOut();
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function authHeaders(): Record<string, string> {
  const s = getSession();
  return s ? { Authorization: `Bearer ${s.token}` } : {};
}

async function request<T>(method: string, path: string, body?: unknown, raw?: Blob): Promise<T> {
  const headers: Record<string, string> = authHeaders();
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (raw !== undefined) headers["Content-Type"] = "application/octet-stream";
  const res = await fetch(path, { method, headers, body: raw ?? (body === undefined ? undefined : JSON.stringify(body)) });
  if (res.status === 401 && !path.endsWith("/auth/login")) {
    signOut();
    throw new ApiError(401, "Your session has ended - please sign in again.");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = (await res.json()) as { detail?: unknown };
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? detail);
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

const q = (params: Record<string, string | number | undefined | null>) => {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => [k, String(v)]);
  const s = new URLSearchParams(entries);
  return s.toString() ? `?${s}` : "";
};

// compile-time check that a templated path exists in the OpenAPI contract
const P = <K extends ApiPath>(template: K, values: Record<string, string> = {}) =>
  (template as string).replace(/\{(\w+)\}/g, (_, k: string) => encodeURIComponent(values[k] ?? ""));

export async function login(username: string, password: string): Promise<Session> {
  const r = await request<{ access_token: string; expires_at: string }>("POST", P("/v1/auth/login"), { username, password });
  const s = { token: r.access_token, user: username, expiresAt: r.expires_at };
  saveSession(s);
  return s;
}

export const api = {
  meta: () => request<Meta>("GET", P("/v1/meta")),
  health: () => request<Row>("GET", P("/v1/health")),
  portfolio: () => request<Portfolio>("GET", P("/v1/portfolio")),
  projects: () => request<Project[]>("GET", P("/v1/projects")),
  createProject: (body: { name: string; business_domain: string; source_technologies: string[];
    target_technologies: string[]; description: string }) => request<Project>("POST", P("/v1/projects"), body),
  overview: (pid: string) => request<Overview>("GET", P("/v1/projects/{project_id}/overview", { project_id: pid })),
  uploads: (pid: string) => request<Upload[]>("GET", P("/v1/projects/{project_id}/uploads", { project_id: pid })),
  upload: (id: string) => request<Upload>("GET", P("/v1/uploads/{upload_id}", { upload_id: id })),
  runs: (pid: string, limit = 20) =>
    request<Row[]>("GET", P("/v1/projects/{project_id}/runs", { project_id: pid }) + q({ limit })),
  startRun: (pid: string, uploadIds: string[]) =>
    request<{ run_id: string }>("POST", P("/v1/projects/{project_id}/runs", { project_id: pid }), { upload_ids: uploadIds }),
  run: (id: string) => request<RunStatus>("GET", P("/v1/runs/{id}", { id })),
  halt: (id: string) => request<Row>("POST", P("/v1/runs/{id}/halt", { id })),
  resume: (id: string) => request<Row>("POST", P("/v1/runs/{id}/resume", { id })),
  goNoGo: (id: string, d: Decision) => request<Row>("POST", P("/v1/runs/{id}/go-no-go", { id }), d),
  artefacts: (runId: string, limit = 500, offset = 0) =>
    request<Row[]>("GET", P("/v1/runs/{id}/artefacts", { id: runId }) + q({ limit, offset })),
  costs: (runId: string) => request<Row>("GET", P("/v1/runs/{id}/costs", { id: runId })),
  readiness: (pid: string, runId?: string) =>
    request<Readiness>("GET", P("/v1/projects/{project_id}/readiness", { project_id: pid }) + q({ run_id: runId })),
  waterfall: (pid: string, runId?: string) =>
    request<{ run_id: string; waterfall: Row[]; live_archive: Row | null; controls: Row | null; change_data: Row[] }>("GET",
      P("/v1/projects/{project_id}/waterfall", { project_id: pid }) + q({ run_id: runId })),
  coverage: (pid: string, runId?: string) =>
    request<{ product_field: string | null; products: { product: string; records: number | null }[]; domains: string[];
      matrix: { domain: string; cells: Record<string, { state: string; rules: number; certified: number }> }[] }>("GET",
      P("/v1/projects/{project_id}/coverage", { project_id: pid }) + q({ run_id: runId })),
  trace: (runId: string, limit = 500) => request<Row[]>("GET", P("/v1/runs/{id}/trace", { id: runId }) + q({ limit })),
  executions: (runId: string) => request<Row[]>("GET", P("/v1/runs/{id}/executions", { id: runId })),
  guardrails: (runId: string) => request<Row[]>("GET", P("/v1/runs/{id}/guardrails", { id: runId })),
  agents: () => request<Row[]>("GET", P("/v1/agents")),
  evals: () => request<Row[]>("GET", P("/v1/evals/latest")),
  dependencies: (pid: string, runId?: string, maxEdges = 600) =>
    request<Graph>("GET", P("/v1/projects/{project_id}/dependencies", { project_id: pid }) + q({ run_id: runId, max_edges: maxEdges })),
  // project-scoped lists: /v1/projects/{id}/<kind>?run_id=&limit=&offset=
  list: (pid: string, kind: string, params: Record<string, string | number | undefined | null> = {}) =>
    request<Row[]>("GET", `/v1/projects/${encodeURIComponent(pid)}/${kind}${q(params)}`),
  liveArchive: (pid: string, runId: string, recommendation?: string, limit = 50, offset = 0) =>
    request<{ total: number; items: Row[] }>("GET", P("/v1/projects/{project_id}/dq/live-archive", { project_id: pid })
      + q({ run_id: runId, recommendation, limit, offset })),
  decideTask: (pid: string, taskId: string, d: Decision) =>
    request<Row>("POST", P("/v1/projects/{project_id}/hitl/tasks/{id}/decision", { project_id: pid, id: taskId }), d),
  ask: (pid: string, question: string, runId?: string) =>
    request<Answer>("POST", P("/v1/projects/{project_id}/assistant/ask", { project_id: pid }), { question, run_id: runId }),
  evidence: (pid: string, type: string, id: string) =>
    request<Row[]>("GET", P("/v1/projects/{project_id}/evidence/{entity_type}/{entity_id}",
      { project_id: pid, entity_type: type, entity_id: id })),
  audit: (pid: string, params: Record<string, string | undefined>) =>
    request<Row[]>("GET", P("/v1/projects/{project_id}/audit", { project_id: pid }) + q(params)),
};

// ------------------------------------------------------------------ chunked, resumable bundle upload (2-4 GB)
export interface UploadProgress { sentBytes: number; totalBytes: number; phase: "uploading" | "validating" | "done" | "failed" }

export async function uploadBundle(pid: string, file: File, systemName: string, systemRole: string,
  onProgress: (p: UploadProgress) => void, signal?: AbortSignal): Promise<Upload> {
  const up = await request<Upload>("POST", P("/v1/projects/{project_id}/uploads", { project_id: pid }),
    { system_name: systemName, system_role: systemRole, filename: file.name, size_bytes: file.size });
  const size = up.chunk_bytes ?? 16 * 1024 * 1024;
  const count = up.chunks_expected ?? Math.ceil(file.size / size);
  let sent = 0;
  let next = 0;
  const worker = async () => {
    while (next < count) {
      if (signal?.aborted) throw new ApiError(0, "upload cancelled");
      const i = next++;
      const blob = file.slice(i * size, Math.min(file.size, (i + 1) * size));
      for (let attempt = 1; ; attempt++) {
        try {
          await request<Row>("PUT", P("/v1/uploads/{upload_id}/chunks/{index}", { upload_id: up.upload_id, index: String(i) }),
            undefined, blob);
          break;
        } catch (e) {
          if (attempt >= 4 || (e instanceof ApiError && e.status >= 400 && e.status < 500)) throw e;
          await new Promise((r) => setTimeout(r, 1000 * attempt));
        }
      }
      sent += blob.size;
      onProgress({ sentBytes: sent, totalBytes: file.size, phase: "uploading" });
    }
  };
  await Promise.all([worker(), worker(), worker()]);
  onProgress({ sentBytes: file.size, totalBytes: file.size, phase: "validating" });
  let cur = await request<Upload>("POST", P("/v1/uploads/{upload_id}/complete", { upload_id: up.upload_id }));
  while (!["ready", "rejected", "failed"].includes(cur.status)) {
    await new Promise((r) => setTimeout(r, 2000));
    cur = await api.upload(up.upload_id);
  }
  onProgress({ sentBytes: file.size, totalBytes: file.size, phase: cur.status === "ready" ? "done" : "failed" });
  return cur;
}

// ------------------------------------------------------------------ run timeline (signals)
export async function pollEvents(runId: string, after: number): Promise<{ events: Row[]; last: number }> {
  const res = await fetch(P("/v1/runs/{id}/events", { id: runId }) + q({ after, once: "true" }), { headers: authHeaders() });
  if (res.status === 401) signOut();
  const text = await res.text();
  const events: Row[] = [];
  let last = after;
  for (const block of text.split("\n\n")) {
    const lines = block.split("\n");
    const data = lines.find((l) => l.startsWith("data: "));
    const id = lines.find((l) => l.startsWith("id: "));
    const ev = lines.find((l) => l.startsWith("event: "));
    if (!data || !id || ev === "event: end") continue;
    events.push(JSON.parse(data.slice(6)) as Row);
    last = Math.max(last, Number(id.slice(4)));
  }
  return { events, last };
}

export const bytes = (n: number | null | undefined): string => {
  if (n === null || n === undefined) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
};
