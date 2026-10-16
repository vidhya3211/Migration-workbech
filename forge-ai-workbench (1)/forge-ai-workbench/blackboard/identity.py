"""Agent identities and the dynamic agent registry.

Agents never call each other: each reads typed items from the blackboard (LangGraph state) and posts a typed
``Signal``. The identity declares everything the control plane enforces at runtime.

Nothing here lists agents. Each agent module declares its own ``IDENTITY`` and registers itself with
``@register_agent``; the registry is discovered at runtime by scanning the ``agents`` package and the
``ai_workbench.agents`` entry-point group (agents shipped in a separate package register the same way). Which
agents actually run for a project is decided by the project's pipeline (``graph.pipeline``), not by code.
"""
from __future__ import annotations

import importlib
import pkgutil
import threading
from collections.abc import Iterator, Mapping

from pydantic import Field, model_validator

from schemas.common import Tier, WorkbenchModel
from schemas.signals import SignalType as S

ENTRY_POINT_GROUP = "ai_workbench.agents"


class AgentIdentity(WorkbenchModel):
    name: str
    tier: Tier
    group: str
    job: str
    allowed_signal_types: frozenset[S]
    allowed_tools: frozenset[str] = Field(default_factory=frozenset)
    allowed_mcp_tools: frozenset[str] = Field(default_factory=frozenset)
    llm_purposes: frozenset[str] = Field(default_factory=frozenset)
    uses_ats: bool = False
    proposes: frozenset[str] = Field(default_factory=frozenset)   # knowledge-core entity types it may propose
    extractor: bool = False            # reads artefacts routed to it by the Cartographer (parallel fan-out)
    source_kind: str | None = None     # extractors: code | jcl | spreadsheet | document | schema
    dialect: str | None = None         # extractors: the canonicaliser the Normaliser applies to its rules
    handles: frozenset[str] = Field(default_factory=frozenset)    # extractors: artefact types routed to it

    @model_validator(mode="after")
    def _tier_capabilities(self) -> "AgentIdentity":
        if self.tier is Tier.T3 and (self.llm_purposes or self.uses_ats):
            raise ValueError(f"{self.name}: T3 agents are deterministic/mechanical only - no LLM, no ATS")
        if self.extractor and not (self.source_kind and self.dialect and self.handles):
            raise ValueError(f"{self.name}: an extractor must declare source_kind, dialect and the artefact types "
                             "it handles")
        return self


def agent_identity(name: str, tier: Tier, group: str, job: str, signals: set[S], *, tools: set[str] = frozenset(),
                   mcp: set[str] = frozenset(), llm: set[str] = frozenset(), ats: bool = False,
                   proposes: set[str] = frozenset(), extractor: bool = False, source_kind: str | None = None,
                   dialect: str | None = None, handles: set[str] = frozenset()) -> AgentIdentity:
    return AgentIdentity(
        name=name, tier=tier, group=group, job=job, allowed_signal_types=frozenset(set(signals) | {S.ORCHESTRATION}),
        allowed_tools=frozenset(tools), allowed_mcp_tools=frozenset(mcp), llm_purposes=frozenset(llm),
        uses_ats=ats, proposes=frozenset(proposes), extractor=extractor, source_kind=source_kind, dialect=dialect,
        handles=frozenset(handles),
    )


class AgentRegistrationError(ValueError):
    pass


_identities: dict[str, AgentIdentity] = {}
_classes: dict[str, type] = {}
_loaded = False
_load_lock = threading.RLock()


def register_agent(cls: type) -> type:
    """Class decorator: registers ``cls.IDENTITY`` with the control plane and binds ``cls.name``."""
    if _loaded is False and not cls.__module__.startswith("agents."):
        load_agents()      # a plug-in registering outside discovery must not shadow a built-in agent
    ident = getattr(cls, "IDENTITY", None)
    if not isinstance(ident, AgentIdentity):
        raise AgentRegistrationError(f"{cls.__name__} must declare IDENTITY = agent_identity(...)")
    existing = _classes.get(ident.name)
    if existing is not None and existing.__qualname__ != cls.__qualname__:
        raise AgentRegistrationError(f"agent name {ident.name!r} is registered twice "
                                     f"({existing.__module__}.{existing.__qualname__} and {cls.__module__})")
    cls.name = ident.name
    _identities[ident.name] = ident
    _classes[ident.name] = cls
    return cls


def load_agents() -> None:
    """Discover every agent: the built-in ``agents`` package plus installed plug-ins (entry points)."""
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        import agents
        for mod in pkgutil.walk_packages(agents.__path__, prefix="agents."):
            importlib.import_module(mod.name)
        try:
            from importlib.metadata import entry_points
            for ep in entry_points(group=ENTRY_POINT_GROUP):
                ep.load()
        except Exception as exc:  # a broken plug-in must be visible, not silently skipped
            raise AgentRegistrationError(f"failed to load agent plug-ins: {exc}") from exc
        _loaded = True


class _Catalog(Mapping[str, AgentIdentity]):
    """Read-only view over the registry; loads agents on first use."""

    def __getitem__(self, key: str) -> AgentIdentity:
        load_agents()
        return _identities[key]

    def __iter__(self) -> Iterator[str]:
        load_agents()
        return iter(sorted(_identities))

    def __len__(self) -> int:
        load_agents()
        return len(_identities)

    def __contains__(self, key: object) -> bool:
        load_agents()
        return key in _identities


AGENTS: Mapping[str, AgentIdentity] = _Catalog()


def get_identity(name: str) -> AgentIdentity:
    try:
        return AGENTS[name]
    except KeyError:
        from blackboard.control_plane import ControlPlaneViolation
        raise ControlPlaneViolation(f"agent '{name}' is not registered with the control plane") from None


def agent_class(name: str) -> type:
    load_agents()
    try:
        return _classes[name]
    except KeyError:
        from blackboard.control_plane import ControlPlaneViolation
        raise ControlPlaneViolation(f"agent '{name}' is not registered with the control plane") from None


def extractors() -> dict[str, type]:
    """Agents the Cartographer can route artefacts to (the applicability fan-out)."""
    load_agents()
    return {n: c for n, c in _classes.items() if _identities[n].extractor}


def extractor_for(artefact_type: str, overrides: dict[str, str] | None = None) -> str | None:
    """The agent an artefact type is routed to: a project routing override first, then the extractor that
    declares it handles the type. ``None`` means the artefact is staged as context only (e.g. data extracts)."""
    load_agents()
    if overrides and artefact_type in overrides:
        name = overrides[artefact_type]
        if name and (name not in _identities or not _identities[name].extractor):
            raise AgentRegistrationError(f"routing override {artefact_type} -> {name}: not a registered extractor")
        return name or None
    owners = sorted(n for n, i in _identities.items() if i.extractor and artefact_type in i.handles)
    if len(owners) > 1:
        raise AgentRegistrationError(f"artefact type {artefact_type!r} is claimed by several extractors: {owners}; "
                                     "set a routing override in the project's heuristics.routing")
    return owners[0] if owners else None
