"""Step registry: every unit of work the orchestration graph can run, self-described.

A *step* is a graph node: it reads named channels from the blackboard (LangGraph state), runs one agent (or a
human gate), persists through the knowledge core and writes named channels back. Steps register themselves
with ``@step``; which steps run, in what order and in parallel with what, is decided by the project's
pipeline definition (``graph.pipeline``) - never by code. New steps ship in the ``graph.nodes`` /
``graph.gates`` modules or in a plug-in package exposing the ``ai_workbench.steps`` entry-point group.

Declared ``reads``/``writes`` are what lets the builder generate the state schema, refuse two parallel lanes
writing the same channel, and tell a pipeline author which inputs a disabled step leaves empty.
"""
from __future__ import annotations

import importlib
import operator
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

ENTRY_POINT_GROUP = "ai_workbench.steps"
StepKind = Literal["node", "fanout", "gate"]


def merge_dict(a: dict | None, b: dict | None) -> dict:
    return {**(a or {}), **(b or {})}


REDUCERS: dict[str, Callable] = {"append": operator.add, "merge": merge_dict}


@dataclass(frozen=True)
class StepSpec:
    name: str
    fn: Callable
    agent: str | None = None                   # identity the step runs under (console + documentation)
    kind: StepKind = "node"
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    reducers: dict[str, str] = field(default_factory=dict)   # channel -> "append" | "merge"
    dispatcher: Callable | None = None         # fanout: state -> list[Send] targeting this step
    applies: Callable | None = None            # (state, services) -> reason it does not apply, or None
    required: bool = False                     # cannot be disabled by a project (control plane, ingest, finalize)
    retry: bool = True
    description: str = ""


class StepRegistrationError(ValueError):
    pass


_steps: dict[str, StepSpec] = {}
_loaded = False
_lock = threading.RLock()


def register_step(spec: StepSpec) -> StepSpec:
    existing = _steps.get(spec.name)
    if existing is not None and existing.fn.__qualname__ != spec.fn.__qualname__:
        raise StepRegistrationError(f"step {spec.name!r} registered twice")
    for ch, red in spec.reducers.items():
        if red not in REDUCERS:
            raise StepRegistrationError(f"step {spec.name}: unknown reducer {red!r} for {ch}")
    _steps[spec.name] = spec
    return spec


def step(name: str, *, agent: str | None = None, kind: StepKind = "node", reads: tuple[str, ...] = (),
         writes: tuple[str, ...] = (), reducers: dict[str, str] | None = None, applies: Callable | None = None,
         required: bool = False, retry: bool = True, description: str = "") -> Callable:
    """Decorator registering a node function as a pipeline step."""
    def deco(fn: Callable) -> Callable:
        register_step(StepSpec(name=name, fn=fn, agent=agent, kind=kind, reads=tuple(reads), writes=tuple(writes),
                               reducers=dict(reducers or {}), applies=applies, required=required, retry=retry,
                               description=description or (fn.__doc__ or "").strip().split("\n")[0]))
        return fn
    return deco


def fanout(name: str, *, agent: str | None = None, dispatcher: Callable, reads: tuple[str, ...] = (),
           writes: tuple[str, ...] = (), reducers: dict[str, str] | None = None, description: str = "") -> Callable:
    """Decorator for a fan-out step: ``dispatcher(state)`` returns one ``Send`` per parallel task."""
    def deco(fn: Callable) -> Callable:
        register_step(StepSpec(name=name, fn=fn, agent=agent, kind="fanout", reads=tuple(reads),
                               writes=tuple(writes), reducers=dict(reducers or {}), dispatcher=dispatcher,
                               description=description or (fn.__doc__ or "").strip().split("\n")[0]))
        return fn
    return deco


def load_steps() -> None:
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        importlib.import_module("graph.nodes")
        importlib.import_module("graph.gates")
        try:
            from importlib.metadata import entry_points
            for ep in entry_points(group=ENTRY_POINT_GROUP):
                ep.load()
        except Exception as exc:
            raise StepRegistrationError(f"failed to load step plug-ins: {exc}") from exc
        _loaded = True


def get_step(name: str) -> StepSpec:
    load_steps()
    try:
        return _steps[name]
    except KeyError:
        raise StepRegistrationError(f"unknown pipeline step {name!r}; registered: {sorted(_steps)}") from None


def all_steps() -> dict[str, StepSpec]:
    load_steps()
    return dict(_steps)


def describe(spec: StepSpec) -> dict[str, Any]:
    return {"name": spec.name, "agent": spec.agent, "kind": spec.kind, "reads": list(spec.reads),
            "writes": list(spec.writes), "required": spec.required, "description": spec.description}
