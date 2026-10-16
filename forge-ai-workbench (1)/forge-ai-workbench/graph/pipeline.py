"""Pipeline definitions -> compiled LangGraph.

    project.pipeline {"name", "disabled_steps", "definition"?}
        -> resolve (definition file / inline definition, minus disabled optional steps)
        -> validate (every step registered, required steps present, parallel lanes write disjoint channels)
        -> snapshot + hash stored on the run
        -> compile: phases in order; parallel lanes join before the next phase; a fan-out step is entered through
           a conditional edge from its predecessor (one ``Send`` per task, or straight on when nothing applies)

The state schema is generated from the steps' declared ``writes`` (with their reducers), so a step that adds a
channel needs no edit to a central state class. Compiled graphs are cached by pipeline hash.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, TypedDict

import yaml
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, Send

from graph.steps import REDUCERS, StepSpec, get_step, load_steps

PIPELINES_DIR = Path(__file__).resolve().parent.parent / "config" / "pipelines"
BASE_CHANNELS: dict[str, str | None] = {"run_id": None, "project_id": None, "upload_ids": None,
                                        "gates": "merge", "facts": "merge"}


class PipelineError(ValueError):
    pass


@dataclass
class WorkbenchContext:
    """Runtime context (not checkpointed): the service container for this process."""
    services: Any


# ------------------------------------------------------------------ definitions
def available_pipelines() -> dict[str, dict]:
    out = {}
    for p in sorted(PIPELINES_DIR.glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        out[d.get("name", p.stem)] = d
    return out


def load_definition(name: str) -> dict:
    path = PIPELINES_DIR / f"{name}.yaml"
    if not path.exists() or not path.resolve().is_relative_to(PIPELINES_DIR.resolve()):
        raise PipelineError(f"unknown pipeline {name!r}; available: {sorted(available_pipelines())}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _phases(defn: dict) -> list[list[list[str]]]:
    """Normalise to phases -> lanes -> step names (a sequential phase is one lane)."""
    out = []
    for i, ph in enumerate(defn.get("phases") or []):
        if "steps" in ph:
            out.append([list(ph["steps"])])
        elif "parallel" in ph:
            out.append([list(lane) for lane in ph["parallel"]])
        else:
            raise PipelineError(f"phase {i} must declare 'steps' or 'parallel'")
    if not out:
        raise PipelineError("a pipeline needs at least one phase")
    return out


def resolve(project_pipeline: dict | None, default_name: str) -> dict:
    """The concrete definition a run executes (a snapshot: phases with disabled steps removed)."""
    load_steps()
    cfg = dict(project_pipeline or {})
    defn = cfg.get("definition") or load_definition(cfg.get("name") or default_name)
    disabled = set(cfg.get("disabled_steps") or [])
    phases = []
    for lanes in _phases(defn):
        kept = [[s for s in lane if s not in disabled] for lane in lanes]
        kept = [lane for lane in kept if lane]
        if kept:
            phases.append(kept)
    snapshot = {"name": defn.get("name", cfg.get("name") or default_name), "version": defn.get("version", 1),
                "phases": phases, "disabled_steps": sorted(disabled)}
    validate(snapshot)
    return snapshot


def pipeline_hash(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot["phases"], sort_keys=True).encode("utf-8")).hexdigest()[:24]


def step_names(snapshot: dict) -> list[str]:
    return [s for lanes in snapshot["phases"] for lane in lanes for s in lane]


def validate(snapshot: dict) -> list[str]:
    """Raises on structural errors; returns advisory warnings (e.g. a step whose inputs nothing produces)."""
    from graph.steps import all_steps
    names = step_names(snapshot)
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise PipelineError(f"steps appear more than once: {sorted(dup)}")
    specs = {n: get_step(n) for n in names}
    for req in (s for s in all_steps().values() if s.required):
        if req.name not in specs:
            raise PipelineError(f"step {req.name!r} is required and cannot be disabled")
    produced: set[str] = set(BASE_CHANNELS)
    warnings: list[str] = []
    for pi, lanes in enumerate(snapshot["phases"]):
        if len(lanes) > 1:
            owners: dict[str, str] = {}
            for lane in lanes:
                for n in lane:
                    sp = specs[n]
                    if sp.kind == "fanout":
                        raise PipelineError(f"fan-out step {n!r} cannot run inside a parallel phase")
                    for ch in sp.writes:
                        if ch in sp.reducers or BASE_CHANNELS.get(ch):
                            continue
                        if ch in owners and owners[ch] != n:
                            raise PipelineError(f"phase {pi}: parallel steps {owners[ch]!r} and {n!r} both write "
                                                f"channel {ch!r}")
                        owners[ch] = n
        phase_out: set[str] = set()
        for lane in lanes:
            seen = set(produced)
            for i, n in enumerate(lane):
                sp = specs[n]
                if sp.kind == "fanout" and n == names[0]:
                    raise PipelineError("a pipeline cannot start with a fan-out step")
                if sp.kind == "fanout" and i == len(lane) - 1:
                    raise PipelineError(f"fan-out step {n!r} must be followed by another step in the same phase")
                missing = [r for r in sp.reads if r not in seen]
                if missing:
                    warnings.append(f"{n} reads {missing} which no earlier step produces (they will be empty)")
                seen |= set(sp.writes)
            phase_out |= seen
        produced |= phase_out
    return warnings


# ------------------------------------------------------------------ state schema + compile
def state_schema(snapshot: dict) -> type:
    channels: dict[str, Any] = {}
    reducers: dict[str, str] = {k: v for k, v in BASE_CHANNELS.items() if v}
    for n in step_names(snapshot):
        sp = get_step(n)
        for ch in sp.writes:
            channels.setdefault(ch, Any)
        reducers.update(sp.reducers)
    for ch in BASE_CHANNELS:
        channels.setdefault(ch, Any)
    fields = {ch: (Annotated[Any, REDUCERS[reducers[ch]]] if ch in reducers else Any) for ch in channels}
    return TypedDict("WorkbenchState", fields, total=False)  # type: ignore[operator]


def _retry() -> RetryPolicy:
    from reasoning.llm.provider import LLMTransientError
    from tools.mcp.client import MCPTransportError
    return RetryPolicy(max_attempts=3, initial_interval=0.5, backoff_factor=2.0,
                       retry_on=(LLMTransientError, MCPTransportError, TimeoutError, ConnectionError))


def node_id(step_name: str) -> str:
    """LangGraph reserves ':' in node names; ``gate:x`` runs as node ``gate_x``."""
    return step_name.replace(":", "_")


def _wrap(spec: StepSpec):
    """Applicability check (stand down with a signal), then the step, then progress bookkeeping."""
    from graph.nodes import mark_step, stand_down

    def node(state, runtime):
        if spec.applies is not None and spec.kind != "fanout":
            reason = spec.applies(state, runtime.context.services)
            if reason:
                stand_down(runtime.context.services, state["run_id"], spec.name, reason)
                mark_step(runtime.context.services, state["run_id"], spec.name)
                return {}
        out = spec.fn(state, runtime)
        run_id = state.get("run_id") if isinstance(state, dict) else None
        if run_id:
            mark_step(runtime.context.services, run_id, spec.name)
        return out
    node.__name__ = node_id(spec.name)
    return node


def compile_pipeline(snapshot: dict, checkpointer=None):
    phases = snapshot["phases"]
    schema = state_schema(snapshot)
    b = StateGraph(schema, context_schema=WorkbenchContext)
    retry = _retry()
    for n in step_names(snapshot):
        sp = get_step(n)
        b.add_node(node_id(n), _wrap(sp), retry_policy=retry if (sp.retry and sp.kind != "gate") else None)

    tails: list[str] = [START]
    pending_fanout: tuple[str, StepSpec] | None = None   # (predecessor node, fan-out step)

    def connect(srcs: list[str], head: str) -> None:
        nonlocal pending_fanout
        if pending_fanout is not None:
            pred, fan = pending_fanout
            pending_fanout = None

            def route(state, _fan=fan, _next=head):
                sends = _fan.dispatcher(state) or []
                return sends or _next
            b.add_conditional_edges(pred, route, [node_id(fan.name), head])
            b.add_edge(node_id(fan.name), head)
            return
        if len(srcs) == 1:
            b.add_edge(srcs[0], head)
        else:
            b.add_edge(srcs, head)

    for lanes in phases:
        new_tails: list[str] = []
        for lane in lanes:
            prev = tails
            for i, n in enumerate(lane):
                sp = get_step(n)
                if sp.kind == "fanout":
                    if len(prev) != 1 or prev[0] == START:
                        raise PipelineError(f"fan-out step {n!r} needs exactly one predecessor step")
                    if pending_fanout is not None:
                        raise PipelineError("two fan-out steps cannot follow each other")
                    pending_fanout = (prev[0], sp)
                    prev = [node_id(n)]
                    continue
                connect(prev, node_id(n))
                prev = [node_id(n)]
            new_tails.extend(prev)
        tails = new_tails
    if pending_fanout is not None:
        raise PipelineError("a fan-out step cannot be the last step of a pipeline")
    connect(tails, END) if tails != [START] else None
    return b.compile(checkpointer=checkpointer, name=f"ai_workbench:{snapshot['name']}")


class GraphCache:
    """Compiled graphs per pipeline hash (compilation is cheap but not free; runs share definitions)."""

    def __init__(self, checkpointer) -> None:
        self.checkpointer = checkpointer
        self._graphs: dict[str, Any] = {}
        self._lock = threading.Lock()

    def get(self, snapshot: dict):
        key = pipeline_hash(snapshot)
        with self._lock:
            g = self._graphs.get(key)
            if g is None:
                g = compile_pipeline(snapshot, self.checkpointer)
                self._graphs[key] = g
            return g


def send(step_name: str, payload: dict) -> Send:
    return Send(step_name, payload)
