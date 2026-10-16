import { useState, type FormEvent } from "react";
import { ApiError, api, type Answer } from "../api/client";
import { ErrorNote } from "./ui";

// Project assistant: answers only from certified knowledge of this project, with citations, or refuses.
export function AssistantDrawer({ pid, projectName, onClose }: { pid: string; projectName: string; onClose: () => void }) {
  const [question, setQuestion] = useState("");
  const [history, setHistory] = useState<{ q: string; a: Answer }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ask = async (e: FormEvent) => {
    e.preventDefault();
    if (!question.trim()) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const a = await api.ask(pid, question.trim());
      setHistory((h) => [{ q: question.trim(), a }, ...h]);
      setQuestion("");
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setNotice("Nothing in this project has been certified yet, so there is nothing the assistant may answer from. "
          + "Ask again once rules are certified in Reviews.");
      } else {
        setError((err as Error).message);
      }
    } finally {
      setBusy(false);
    }
  };
  return (
    <aside className="drawer">
      <header>
        <div>
          <div className="eyebrow">ASSISTANT</div>
          <h3 style={{ margin: "4px 0" }}>{projectName || "Project"}</h3>
        </div>
        <button className="secondary" onClick={onClose}>Close</button>
      </header>
      <p className="muted small">Answers come only from rules, decisions and mappings a named person has certified in this project - with citations - or the assistant says it cannot answer.</p>
      <form onSubmit={ask} className="toolbar">
        <input style={{ flex: 1 }} placeholder="e.g. What is the certified policy administration fee?" value={question}
          onChange={(e) => setQuestion(e.target.value)} />
        <button disabled={busy}>{busy ? "..." : "Ask"}</button>
      </form>
      <ErrorNote error={error} />
      {notice && <div className="note">{notice}</div>}
      {history.map(({ q, a }, i) => (
        <div key={i} className="card">
          <div className="muted small">{q}</div>
          {a.refused ? <div className="note">Cannot answer from certified knowledge: {a.refusal_reason}</div>
            : <p className="answer">{a.answer}</p>}
          {a.citations?.length ? (
            <div className="small muted">Sources: {(a.citations as unknown[]).map((c) => typeof c === "string" ? c : JSON.stringify(c)).join(" · ")}</div>
          ) : null}
        </div>
      ))}
    </aside>
  );
}
