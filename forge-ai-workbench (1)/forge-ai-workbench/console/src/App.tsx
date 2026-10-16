import { useEffect, useState } from "react";
import { api, getSession, signOut, whenSignedOut, type Meta, type Project, type Session } from "./api/client";
import { AssistantDrawer } from "./components/AssistantDrawer";
import { PageBoundary } from "./components/ui";
import { LoginPage } from "./pages/LoginPage";
import { AssessmentPage } from "./pages/AssessmentPage";
import { AssetsPage } from "./pages/AssetsPage";
import { DependenciesPage } from "./pages/DependenciesPage";
import { OverviewPage } from "./pages/OverviewPage";
import { PortfolioPage } from "./pages/PortfolioPage";
import { ReadinessPage } from "./pages/ReadinessPage";
import { RecommendationsPage } from "./pages/RecommendationsPage";
import { ReconcilePage } from "./pages/ReconcilePage";
import { ReviewsPage } from "./pages/ReviewsPage";
import { RoadmapPage } from "./pages/RoadmapPage";
import { AuditPage, EvalsPage, RuntimePage } from "./pages/RuntimePage";

const NAV: { section: string; items: { page: string; label: string; icon: string; project: boolean }[] }[] = [
  { section: "WORKBENCH", items: [
    { page: "portfolio", label: "Portfolio", icon: "▦", project: false },
    { page: "overview", label: "Overview", icon: "◎", project: true }] },
  { section: "ASSESSMENT", items: [
    { page: "assets", label: "Assets", icon: "▤", project: true },
    { page: "readiness", label: "Readiness", icon: "◔", project: true },
    { page: "assessment", label: "Assessment", icon: "☰", project: true },
    { page: "dependencies", label: "Dependencies", icon: "⛓", project: true }] },
  { section: "DECISIONS", items: [
    { page: "reviews", label: "Reviews", icon: "✓", project: true },
    { page: "reconcile", label: "Reconcile", icon: "⇄", project: true },
    { page: "recommendations", label: "Recommendations", icon: "✦", project: true },
    { page: "roadmap", label: "Roadmap", icon: "➜", project: true }] },
  { section: "GOVERNANCE", items: [
    { page: "runtime", label: "Runtime", icon: "⚙", project: true },
    { page: "audit", label: "Audit trail", icon: "⌗", project: true },
    { page: "evals", label: "Evaluations", icon: "✔", project: true }] },
];

function useRoute(): [{ page: string; pid: string | null }, (page: string, pid?: string | null) => void] {
  const parse = () => {
    const h = window.location.hash.replace(/^#\/?/, "");
    const m = h.match(/^p\/([^/]+)\/([a-z]+)/);
    return m ? { page: m[2], pid: decodeURIComponent(m[1]) } : { page: "portfolio", pid: null };
  };
  const [route, setRoute] = useState(parse);
  useEffect(() => {
    const on = () => setRoute(parse());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const go = (page: string, pid?: string | null) => {
    window.location.hash = pid ? `/p/${encodeURIComponent(pid)}/${page}` : "/portfolio";
  };
  return [route, go];
}

export function App() {
  const [session, setSession] = useState<Session | null>(getSession());
  useEffect(() => whenSignedOut(() => setSession(null)), []);
  if (!session) return <LoginPage onSignedIn={setSession} />;
  return <Shell session={session} />;
}

function Shell({ session }: { session: Session }) {
  const [route, go] = useRoute();
  const [meta, setMeta] = useState<Meta | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [healthy, setHealthy] = useState(true);
  const [menu, setMenu] = useState(false);
  const [assistant, setAssistant] = useState(false);
  const pid = route.pid;

  const refreshProjects = () => api.projects().then(setProjects).catch(() => undefined);
  useEffect(() => {
    api.meta().then((m) => { setMeta(m); document.title = m.product_name; }).catch(() => undefined);
    refreshProjects();
    const ping = () => api.health().then(() => setHealthy(true)).catch(() => setHealthy(false));
    ping();
    const t = setInterval(ping, 30000);
    return () => clearInterval(t);
  }, []);

  const initials = session.user.slice(0, 2).toUpperCase();
  const project = projects.find((p) => p.project_id === pid) ?? null;
  const open = (id: string) => go("overview", id);

  let page;
  if (!pid || route.page === "portfolio") {
    page = <PortfolioPage meta={meta} open={open} onCreated={(id) => { refreshProjects(); open(id); }} />;
  } else {
    const props = { pid, project, go: (p: string) => go(p, pid) };
    page = {
      overview: <OverviewPage {...props} />,
      assets: <AssetsPage {...props} />,
      readiness: <ReadinessPage {...props} />,
      assessment: <AssessmentPage {...props} />,
      dependencies: <DependenciesPage {...props} />,
      reviews: <ReviewsPage {...props} />,
      reconcile: <ReconcilePage {...props} />,
      recommendations: <RecommendationsPage {...props} />,
      roadmap: <RoadmapPage {...props} />,
      runtime: <RuntimePage {...props} />,
      audit: <AuditPage {...props} />,
      evals: <EvalsPage />,
    }[route.page] ?? <OverviewPage {...props} />;
  }

  return (
    <div className="shell">
      <header className="topbar">
        {meta?.organisation_name && <div className="org">{meta.organisation_name.toUpperCase()}</div>}
        <div className="product">{meta?.product_name ?? "Migration Workbench"}</div>
        <select value={pid ?? ""} onChange={(e) => (e.target.value ? open(e.target.value) : go("portfolio"))}
          aria-label="Select a project">
          <option value="">Select a project...</option>
          {projects.map((p) => <option key={p.project_id} value={p.project_id}>{p.name}</option>)}
        </select>
        <div className="grow" />
        <span className={`dot ${healthy ? "" : "off"}`} title={healthy ? "API healthy" : "API unreachable"} />
        <button className="avatar ai" title="Ask the assistant" onClick={() => setAssistant(true)} disabled={!pid}>AI</button>
        <button className="avatar" title={session.user} onClick={() => setMenu(!menu)}>{initials}</button>
        {menu && (
          <div className="menu" onMouseLeave={() => setMenu(false)}>
            <div><strong>{session.user}</strong></div>
            <div className="muted small">Administrator · session until {new Date(session.expiresAt).toLocaleTimeString()}</div>
            <div style={{ marginTop: 10 }}><button className="secondary" onClick={signOut}>Sign out</button></div>
          </div>
        )}
      </header>
      <nav className="sidebar">
        {NAV.map((s) => (
          <div key={s.section}>
            <div className="section">{s.section}</div>
            {s.items.map((it) => {
              const active = route.page === it.page || (it.page === "portfolio" && !pid);
              const disabled = it.project && !pid;
              return (
                <a key={it.page} href={it.project ? (pid ? `#/p/${encodeURIComponent(pid)}/${it.page}` : undefined) : "#/portfolio"}
                  className={`${active ? "active" : ""} ${disabled ? "disabled" : ""}`} title={disabled ? "Select a project first" : ""}>
                  <span className="icon">{it.icon}</span>{it.label}
                </a>
              );
            })}
          </div>
        ))}
      </nav>
      <main className="content"><PageBoundary resetKey={`${route.page}:${pid ?? ""}`}>{page}</PageBoundary></main>
      {pid && !assistant && <button className="fab" onClick={() => setAssistant(true)} title="Ask the assistant">AI</button>}
      {assistant && pid && <AssistantDrawer pid={pid} projectName={project?.name ?? ""} onClose={() => setAssistant(false)} />}
    </div>
  );
}

export interface PageProps {
  pid: string;
  project: Project | null;
  go: (page: string) => void;
}
