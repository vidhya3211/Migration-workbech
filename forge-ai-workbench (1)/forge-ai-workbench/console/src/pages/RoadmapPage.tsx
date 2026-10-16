import type { PageProps } from "../App";
import { api, type RunStatus } from "../api/client";
import { NoRun, RunPicker, useRuns } from "../components/RunPicker";
import { Chip, ErrorNote, PageHead, Progress, useLoad } from "../components/ui";

// The project's assessment plan: the pipeline phases this project runs, what is done, where it waits for a
// human, and what comes next.
type Phase = (string | string[] | string[][])[];

const label = (s: string) => s.replace(/^gate:/, "").replace(/_/g, " ");

export function RoadmapPage({ pid, go }: PageProps) {
  const [runs, runId, setRunId] = useRuns(pid);
  const [run, error] = useLoad<RunStatus | null>(() => (runId ? api.run(runId) : Promise.resolve(null)), [runId], null);
  if (!runs.length) return <section><PageHead title="Roadmap" /><NoRun /></section>;
  const done = new Set(run?.progress.completed ?? []);
  const phases = (run?.pipeline.phases ?? []) as Phase[];
  const flat = (p: Phase): string[] => p.flatMap((x) => (Array.isArray(x) ? (x as (string | string[])[]).flatMap((y) => (Array.isArray(y) ? y : [y])) : [x]));
  let currentFound = false;
  return (
    <section>
      <PageHead title="Roadmap" sub={run ? `Pipeline ${run.pipeline.name} · ${run.progress.steps_completed} of ${run.progress.steps_total} steps` : ""}>
        <RunPicker runs={runs} runId={runId} onChange={setRunId} />
      </PageHead>
      <ErrorNote error={error} />
      {run && <div className="card"><Progress ratio={run.progress.ratio} />
        {run.current_gate && <p>Waiting for a decision at <strong>{label(run.current_gate)}</strong> <button className="link" onClick={() => go("reviews")}>open reviews</button></p>}</div>}
      <div className="phases">
        {phases.map((p, i) => {
          const steps = flat(p);
          const complete = steps.every((s) => done.has(s));
          const current = !complete && !currentFound;
          if (current) currentFound = true;
          const gates = steps.filter((s) => s.startsWith("gate:"));
          return (
            <div key={i} className={`phase card ${complete ? "done" : current ? "current" : ""}`}>
              <div className="num">{complete ? "✓" : i + 1}</div>
              <div>
                <strong>Phase {i + 1}</strong>{" "}
                {gates.length ? <span className="muted small">human gate: {gates.map(label).join(", ")}</span> : null}
                <div className="steps">
                  {steps.map((s) => <Chip key={s} value={done.has(s) ? `${label(s)} ✓` : s === `gate:${run?.current_gate}` ? `${label(s)} - waiting` : label(s)} />)}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
