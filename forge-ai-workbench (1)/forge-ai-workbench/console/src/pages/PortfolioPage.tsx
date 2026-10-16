import { useState, type FormEvent } from "react";
import { api, type Meta, type Portfolio, type PortfolioRow } from "../api/client";
import { Chip, ErrorNote, Kpi, PageHead, Progress, Table, pct, useLoad } from "../components/ui";

const tech = (r: PortfolioRow) => {
  const src = r.source_technologies.join(", ");
  const tgt = r.target_technologies.join(", ");
  return src && tgt ? `${src} to ${tgt}` : src || tgt || r.business_domain;
};

export function PortfolioPage({ meta, open, onCreated }: { meta: Meta | null; open: (pid: string) => void; onCreated: (pid: string) => void }) {
  const [data, error, reload] = useLoad<Portfolio | null>(() => api.portfolio(), [], null);
  const [creating, setCreating] = useState(false);
  const k = data?.kpis;
  return (
    <section>
      <PageHead title="Project Portfolio" sub="Manage multiple assessments and open the next priority project">
        <button onClick={() => setCreating(true)}>+ Create Project</button>
      </PageHead>
      <ErrorNote error={error} />
      <div className="kpis">
        <Kpi label="Active projects" value={k?.active_projects ?? "—"} sub={`${k?.in_assessment ?? 0} in assessment`} />
        <Kpi label="Average readiness" value={pct(k?.average_readiness)} sub={`${k?.assessed ?? 0} of ${k?.active_projects ?? 0} assessed`} />
        <Kpi label="Critical risks" value={k?.critical_risks ?? "—"} sub={`${k?.high_severity ?? 0} high severity`} tone={k?.critical_risks ? "bad" : undefined} />
        <Kpi label="Pending approvals" value={k?.pending_approvals ?? "—"} sub={`${k?.low_confidence ?? 0} low confidence`} tone={k?.pending_approvals ? "warn" : undefined} />
      </div>
      <Table title="Migration projects" rows={(data?.projects ?? []) as unknown as Record<string, unknown>[]}
        empty="No projects yet - create one to start an assessment."
        onRow={(r) => open(String(r.project_id))}
        columns={[
          { key: "project", render: (r) => {
            const row = r as unknown as PortfolioRow;
            return <div><div className="pname">{row.name}</div><div className="psub">{tech(row)}</div></div>;
          } },
          { key: "stage", render: (r) => <Chip value={r.stage} /> },
          { key: "progress", render: (r) => <Progress ratio={Number(r.progress ?? 0)} /> },
          { key: "readiness", render: (r) => pct(r.readiness as number | null) },
          { key: "risks", render: (r) => <span className={Number(r.risks) ? "chip bad" : "muted"}>{String(r.risks)}</span> },
          { key: "open", label: "", render: (r) => <button className="secondary" onClick={(e) => { e.stopPropagation(); open(String(r.project_id)); }}>Open</button> },
        ]} />
      {creating && <CreateProjectModal domains={meta?.business_domains ?? ["Policy", "Claims", "Finance", "Customer", "Distribution", "Other"]}
        onClose={() => setCreating(false)} onCreated={(id) => { setCreating(false); reload(); onCreated(id); }} />}
    </section>
  );
}

function CreateProjectModal({ domains, onClose, onCreated }: { domains: string[]; onClose: () => void; onCreated: (pid: string) => void }) {
  const [name, setName] = useState("");
  const [domain, setDomain] = useState(domains[0] ?? "Policy");
  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const split = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const p = await api.createProject({ name: name.trim(), business_domain: domain, source_technologies: split(source),
        target_technologies: split(target), description: description.trim() });
      onCreated(p.project_id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="overlay" onClick={onClose}>
      <form className="modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="eyebrow">NEW WORKSPACE</div>
        <h2>Create Project</h2>
        <label className="field">Project name
          <input placeholder="New Migration Assessment" value={name} onChange={(e) => setName(e.target.value)} required autoFocus maxLength={200} />
        </label>
        <label className="field">Business domain
          <select value={domain} onChange={(e) => setDomain(e.target.value)}>
            {domains.map((d) => <option key={d}>{d}</option>)}
          </select>
        </label>
        <label className="field">Source technologies
          <input placeholder="COBOL, JCL, PL/SQL" value={source} onChange={(e) => setSource(e.target.value)} />
        </label>
        <label className="field"><span>Target technologies <span className="hint">(optional)</span></span>
          <input placeholder="PHP, PostgreSQL" value={target} onChange={(e) => setTarget(e.target.value)} />
        </label>
        <label className="field"><span>Description <span className="hint">(optional)</span></span>
          <textarea placeholder="Scope, systems in play, anything reviewers should know" value={description}
            onChange={(e) => setDescription(e.target.value)} maxLength={4000} />
        </label>
        <ErrorNote error={error} />
        <div className="actions">
          <button type="button" className="secondary" onClick={onClose}>Cancel</button>
          <button type="submit" disabled={busy || !name.trim()}>{busy ? "Creating..." : "Create Project"}</button>
        </div>
      </form>
    </div>
  );
}
