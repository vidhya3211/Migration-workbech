import { api, type Row } from "../api/client";
import { Json, Table, useLoad } from "./ui";

// Evidence / provenance viewer: the exact locator and excerpt behind a fact, plus its audit trail.
export function EvidenceDrawer({ pid, entityType, entityId, onClose }: {
  pid: string; entityType: string; entityId: string; onClose: () => void;
}) {
  const [prov] = useLoad<Row[]>(() => api.evidence(pid, entityType, entityId), [pid, entityType, entityId], []);
  const [audit] = useLoad<Row[]>(() => api.audit(pid, { entity_type: entityType, entity_id: entityId }),
    [pid, entityType, entityId], []);
  return (
    <aside className="drawer">
      <header>
        <h3>{entityType.replace(/_/g, " ")} <code>{entityId}</code></h3>
        <button className="secondary" onClick={onClose}>Close</button>
      </header>
      <h4>Evidence</h4>
      {prov.length === 0 && <p className="muted">No evidence rows.</p>}
      {prov.map((p, i) => {
        const loc = (p.locator ?? {}) as Row;
        return (
          <div key={i} className="evidence">
            <div className="muted small">
              {String(p.system_name)} ({String(p.system_role)}) - {String(loc.kind)}: {String(loc.file)}
              {loc.line_start ? ` lines ${String(loc.line_start)}-${String(loc.line_end)}` : ""}
              {loc.sheet ? ` ${String(loc.sheet)}!${String(loc.cell_range)}` : ""}
              {loc.row_number ? ` row ${String(loc.row_number)}` : ""}
            </div>
            <pre className="excerpt">{String(p.excerpt)}</pre>
            <div className="muted small">sha256 {String(p.source_sha256).slice(0, 16)}</div>
          </div>
        );
      })}
      <h4>Audit trail</h4>
      <Table rows={audit} columns={[{ key: "when" }, { key: "who" }, { key: "actor_type" }, { key: "what" }, { key: "reason" }]} />
      {audit.length > 0 && <details><summary>raw</summary><Json value={audit} /></details>}
    </aside>
  );
}
