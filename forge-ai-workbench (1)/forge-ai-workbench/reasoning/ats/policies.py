"""Concrete ATS search policies used by the agents.

* ``MappingAssignmentPolicy``   - ambiguous mappings: one-to-one assignment of source to target attributes
* ``RootCausePolicy``           - root-cause hypotheses for a data-quality cohort
* ``DifferentialCausePolicy``   - alternative interpretations of *why* source and target diverge
* ``BreakCausePolicy``          - which differential (if any) explains a reconciliation break cluster
* ``EntityClusterPolicy``       - resolving duplicate-customer clusters with non-transitive pair scores
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Hashable

from reasoning.ats.node import ATSNode, Candidate
from reasoning.ats.scoring import evidence_weighted, mean
from reasoning.deterministic.expressions import ExpressionError, identifiers
from schemas.reasoning import HeuristicResult


# ------------------------------------------------------------------------------------------ mapping
class MappingAssignmentPolicy:
    """State: {"assigned": {src: tgt|None}}; constraint: each target used at most once."""

    name = "mapping.assignment"

    def __init__(self, sources: list[str], candidates: dict[str, list[HeuristicResult]], top_k: int = 3,
                 taken: set[str] | None = None) -> None:
        self.sources = sources
        self.taken = taken or set()   # targets already assigned outside the ambiguous set
        self.candidates = {s: sorted(c, key=lambda h: -h.score)[:top_k] for s, c in candidates.items()}
        self._scores = {(s, h.candidate.split("->", 1)[1]): h.score for s, cs in self.candidates.items() for h in cs}

    def root(self) -> Candidate:
        return Candidate(hypothesis="no attributes assigned", state={"assigned": {}})

    def expand(self, node: ATSNode) -> list[Candidate]:
        assigned: dict = node.state["assigned"]
        pending = [s for s in self.sources if s not in assigned]
        if not pending:
            return []
        src = pending[0]
        out = []
        for h in self.candidates.get(src, []):
            tgt = h.candidate.split("->", 1)[1]
            out.append(Candidate(hypothesis=f"{src} -> {tgt}", state={"assigned": {**assigned, src: tgt}},
                                 evidence=[f"{h.heuristic} {h.score:.2f}"], actions=[f"assign {src} to {tgt}"],
                                 source="heuristic"))
        out.append(Candidate(hypothesis=f"{src} -> (unmapped)", state={"assigned": {**assigned, src: None}},
                             evidence=["leave unmapped for the data steward"], actions=[f"leave {src} unmapped"]))
        return out

    def score(self, node: ATSNode) -> tuple[float, float]:
        assigned = node.state["assigned"]
        vals = [self._scores.get((s, t), 0.0) if t else 0.3 for s, t in assigned.items()]
        remaining = [max((h.score for h in self.candidates.get(s, [])), default=0.3)
                     for s in self.sources if s not in assigned]
        score = mean(vals + remaining) if (vals or remaining) else 0.0      # optimistic bound for open nodes
        return score, mean(vals) if vals else 0.0

    def violates(self, node: ATSNode) -> str | None:
        used = [t for t in node.state["assigned"].values() if t]
        dup = {t for t in used if used.count(t) > 1} | (set(used) & self.taken)
        return f"target(s) assigned twice: {sorted(dup)}" if dup else None

    def is_terminal(self, node: ATSNode) -> bool:
        return len(node.state["assigned"]) == len(self.sources)

    def state_key(self, node: ATSNode) -> Hashable:
        return tuple(sorted(node.state["assigned"].items(), key=lambda kv: kv[0]))


# ------------------------------------------------------------------------------------------ root cause
class RootCausePolicy:
    """Hypotheses from the known-cause catalogue, tested against the cohort's deterministic facts."""

    name = "dq.root_cause"

    def __init__(self, defect: str, facts: dict, catalogue: list[dict]) -> None:
        self.defect = defect
        self.facts = facts
        self.catalogue = catalogue

    def root(self) -> Candidate:
        return Candidate(hypothesis=f"cause of {self.defect}", state={"cause": None, "refined": False})

    def expand(self, node: ATSNode) -> list[Candidate]:
        if node.state["cause"] is None:
            out = []
            # a cause whose signature does not cover the defect can never win (it scores ~0.05): only expand the
            # matching ones - the whole catalogue only when none matches, so a growing catalogue costs nothing
            matching = [c for c in self.catalogue if self.defect in c["signatures"]]
            for c in matching or self.catalogue:
                ev = []
                if self.defect in c["signatures"]:
                    ev.append(f"catalogue signature matches defect {self.defect}")
                out.append(Candidate(hypothesis=c["code"], state={"cause": c["code"], "refined": False},
                                     evidence=ev, actions=[f"test {c['code']}"]))
            out.append(Candidate(hypothesis="RC-UNKNOWN", state={"cause": "RC-UNKNOWN", "refined": False},
                                 evidence=["no catalogue cause fits - escalate"], actions=["escalate"]))
            return out
        if not node.state["refined"]:
            ev = list(node.evidence)
            cause = node.state["cause"]
            sentinel = self.facts.get("sentinel_values") or []
            if cause == "RC-LEGACY-DEFAULT" and sentinel:
                ev.append(f"sentinel value(s) observed: {sentinel}")
            if cause == "RC-DATE-FORMAT" and self.facts.get("parseable_other_format"):
                ev.append("values parse under an alternative date format")
            if cause == "RC-CODE-DRIFT" and self.facts.get("unknown_codes"):
                ev.append(f"codes outside the certified domain: {self.facts['unknown_codes']}")
            if cause == "RC-RANGE" and self.facts.get("range_violation"):
                ev.append(f"range violation: {self.facts['range_violation']}")
            return [Candidate(hypothesis=cause, state={"cause": cause, "refined": True}, evidence=ev,
                              actions=[f"confirm {cause} with data steward"])]
        return []

    def score(self, node: ATSNode) -> tuple[float, float]:
        cause = node.state["cause"]
        if cause is None:
            return 1.0, 0.0
        if cause == "RC-UNKNOWN":
            return 0.2, 0.2 if node.state["refined"] else 0.0
        entry = next((c for c in self.catalogue if c["code"] == cause), None)
        sig = 1.0 if entry and self.defect in entry["signatures"] else 0.1
        s = round(0.5 * sig + 0.5 * min(1.0, len(node.evidence) / 2), 4)
        return s, evidence_weighted(s, len(node.evidence)) if node.state["refined"] else 0.0

    def violates(self, node: ATSNode) -> str | None:
        return None

    def is_terminal(self, node: ATSNode) -> bool:
        return bool(node.state["refined"])

    def state_key(self, node: ATSNode) -> Hashable:
        return (node.state["cause"], node.state["refined"])


# ------------------------------------------------------------------------------------------ differential
class DifferentialCausePolicy:
    """Why does V diverge? Tests competing interpretations deterministically against the evidence."""

    name = "differential.interpretation"

    def __init__(self, variable: str, comparison: dict, source_steps: list[dict], target_steps: list[dict]) -> None:
        self.variable = variable
        self.cmp = comparison
        self.src = source_steps
        self.tgt = target_steps

    def _vars(self, steps: list[dict], key: str) -> set[str]:
        out: set[str] = set()
        for s in steps:
            for src in ([s["expr"]] if key == "expr" else s["conditions"]):
                try:
                    out |= identifiers(src)
                except ExpressionError:
                    pass
        return out - {self.variable}

    def root(self) -> Candidate:
        return Candidate(hypothesis=f"why {self.variable} diverges", state={"h": None})

    def expand(self, node: ATSNode) -> list[Candidate]:
        if node.state["h"] is not None:
            return []
        out: list[Candidate] = []
        s_expr, t_expr = self._vars(self.src, "expr"), self._vars(self.tgt, "expr")
        s_cond, t_cond = self._vars(self.src, "cond"), self._vars(self.tgt, "cond")
        div = self.cmp.get("divergent", [])
        cat = self.cmp.get("category")
        ev_const = [f"source {d['source']} vs target {d['target']}" for d in div[:1]]
        out.append(Candidate(hypothesis="constant value differs", state={"h": "value"},
                             evidence=ev_const if cat == "value" else []))
        missing = sorted(s_expr - t_expr)
        extra = sorted(t_expr - s_expr)
        if missing:
            out.append(Candidate(hypothesis=f"target omits term(s) {', '.join(missing)}", state={"h": "missing_term"},
                                 evidence=[f"source expression uses {missing}; target does not"]))
        if extra:
            out.append(Candidate(hypothesis=f"target adds term(s) {', '.join(extra)}", state={"h": "extra_term"},
                                 evidence=[f"target expression uses {extra}; source does not"]))
        boundary = self._boundary_evidence(div, s_cond | t_cond)
        out.append(Candidate(hypothesis="condition boundary differs", state={"h": "boundary"},
                             evidence=boundary if cat == "condition" else []))
        out.append(Candidate(hypothesis="rounding / precision differs", state={"h": "rounding"},
                             evidence=[f"max delta {self.cmp.get('max_abs_delta')}"] if cat == "rounding" else []))
        out.append(Candidate(hypothesis="expression logic differs", state={"h": "expression"},
                             evidence=["expressions evaluate differently"] if cat == "expression" else []))
        return out

    def _boundary_evidence(self, div: list[dict], cond_vars: set[str]) -> list[str]:
        ev = []
        for v in sorted(cond_vars):
            vals = {d["inputs"].get(v) for d in div if d["inputs"].get(v) is not None}
            if len(vals) == 1:
                ev.append(f"all divergent vectors have {v} = {next(iter(vals))} (a single boundary value)")
        return ev

    def score(self, node: ATSNode) -> tuple[float, float]:
        if node.state["h"] is None:
            return 1.0, 0.0
        s = min(1.0, 0.45 + 0.3 * len(node.evidence)) if node.evidence else 0.05
        return round(s, 4), round(s, 4)

    def violates(self, node: ATSNode) -> str | None:
        return None

    def is_terminal(self, node: ATSNode) -> bool:
        return node.state["h"] is not None

    def state_key(self, node: ATSNode) -> Hashable:
        return node.state["h"]


# ------------------------------------------------------------------------------------------ break clusters
class BreakCausePolicy:
    """Which differential explains this cluster - or is it a data / transformation problem?"""

    name = "reconciliation.break_cause"

    def __init__(self, cluster: dict, link_scores: list[HeuristicResult]) -> None:
        self.cluster = cluster
        self.links = {h.candidate.split("<-", 1)[1]: h for h in link_scores}

    def root(self) -> Candidate:
        return Candidate(hypothesis=f"cause of {self.cluster['signature']}", state={"cause": None})

    def expand(self, node: ATSNode) -> list[Candidate]:
        if node.state["cause"] is not None:
            return []
        out = [Candidate(hypothesis=f"differential {did}", state={"cause": did}, evidence=h.evidence if h.passed else [],
                         source="heuristic") for did, h in self.links.items()]
        uniform = self.cluster.get("uniform_delta")
        out.append(Candidate(hypothesis="transformation / load defect", state={"cause": "load_defect"},
                             evidence=["delta is not uniform across the cluster"] if not uniform else []))
        out.append(Candidate(hypothesis="record not loaded", state={"cause": "missing"},
                             evidence=["break type missing_in_target"] if self.cluster.get("type") == "missing_in_target" else []))
        return out

    def score(self, node: ATSNode) -> tuple[float, float]:
        cause = node.state["cause"]
        if cause is None:
            return 1.0, 0.0
        if cause in self.links:
            s = self.links[cause].score
        else:
            s = 0.7 if node.evidence else 0.1
        return s, s if node.evidence else s * 0.3

    def violates(self, node: ATSNode) -> str | None:
        return None

    def is_terminal(self, node: ATSNode) -> bool:
        return node.state["cause"] is not None

    def state_key(self, node: ATSNode) -> Hashable:
        return node.state["cause"]


# ------------------------------------------------------------------------------------------ replay attribution
class ReplayAttributionPolicy:
    """Which set of differentials explains a break? State = the differentials whose *source* definition is
    swapped into the target logic; a node is proven when replaying the record reproduces the source value."""

    name = "reconciliation.replay_attribution"

    def __init__(self, candidates: list[str], replay, source_value: Decimal, max_set: int = 3) -> None:
        self.candidates = candidates
        self.replay = replay                  # callable(frozenset[str]) -> Decimal | None
        self.source_value = source_value
        self.max_set = max_set
        self._cache: dict[frozenset, Decimal | None] = {}

    def _value(self, toggled: frozenset) -> Decimal | None:
        if toggled not in self._cache:
            self._cache[toggled] = self.replay(toggled)
        return self._cache[toggled]

    def root(self) -> Candidate:
        return Candidate(hypothesis="target logic as implemented", state={"toggled": []})

    def expand(self, node: ATSNode) -> list[Candidate]:
        current = set(node.state["toggled"])
        if len(current) >= self.max_set or self._exact(node):
            return []
        out = []
        for c in self.candidates:
            if c in current:
                continue
            toggled = sorted(current | {c})
            v = self._value(frozenset(toggled))
            ev = [f"replay with source rule(s) for {', '.join(toggled)} -> {v}"] if v is not None else []
            out.append(Candidate(hypothesis=" + ".join(toggled), state={"toggled": toggled}, evidence=ev,
                                 actions=[f"swap in the source definition of {c}"]))
        return out

    def _exact(self, node: ATSNode) -> bool:
        v = self._value(frozenset(node.state["toggled"]))
        return v is not None and v == self.source_value

    def score(self, node: ATSNode) -> tuple[float, float]:
        v = self._value(frozenset(node.state["toggled"]))
        if v is None:
            return 0.0, 0.0
        base = abs(self._value(frozenset()) - self.source_value) if self._value(frozenset()) is not None else None
        gap = abs(v - self.source_value)
        closeness = 1.0 if gap == 0 else float(1 - min(Decimal(1), gap / base)) if base else 0.0
        parsimony = 1.0 - 0.1 * len(node.state["toggled"])
        s = round(max(0.0, closeness * parsimony), 4)
        return s, (s if gap == 0 else 0.0)

    def violates(self, node: ATSNode) -> str | None:
        return None

    def is_terminal(self, node: ATSNode) -> bool:
        return self._exact(node) or len(node.state["toggled"]) >= self.max_set

    def state_key(self, node: ATSNode) -> Hashable:
        return tuple(node.state["toggled"])


# ------------------------------------------------------------------------------------------ entity clusters
class EntityClusterPolicy:
    """Partition candidate duplicates into clusters; pairs below threshold may not share a cluster."""

    name = "entity.cluster"

    def __init__(self, members: list[str], pair_scores: dict[tuple[str, str], float], threshold: float) -> None:
        self.members = sorted(members)
        self.pairs = {tuple(sorted(k)): v for k, v in pair_scores.items()}
        self.threshold = threshold

    def _p(self, a: str, b: str) -> float:
        return self.pairs.get(tuple(sorted((a, b))), 0.0)

    def root(self) -> Candidate:
        return Candidate(hypothesis="no members placed", state={"clusters": []})

    def expand(self, node: ATSNode) -> list[Candidate]:
        clusters: list[list[str]] = node.state["clusters"]
        placed = {m for c in clusters for m in c}
        pending = [m for m in self.members if m not in placed]
        if not pending:
            return []
        m = pending[0]
        out = []
        for i, c in enumerate(clusters):
            new = [list(x) for x in clusters]
            new[i] = sorted(c + [m])
            out.append(Candidate(hypothesis=f"{m} joins {c}", state={"clusters": new},
                                 evidence=[f"min pair score {min(self._p(m, x) for x in c):.2f}"]))
        out.append(Candidate(hypothesis=f"{m} is distinct", state={"clusters": [list(x) for x in clusters] + [[m]]},
                             evidence=["kept separate"]))
        return out

    def score(self, node: ATSNode) -> tuple[float, float]:
        vals = []
        for c in node.state["clusters"]:
            for i, a in enumerate(c):
                for b in c[i + 1:]:
                    vals.append(self._p(a, b))
        s = mean(vals) if vals else 0.6
        return s, s

    def violates(self, node: ATSNode) -> str | None:
        for c in node.state["clusters"]:
            for i, a in enumerate(c):
                for b in c[i + 1:]:
                    if self._p(a, b) < self.threshold:
                        return f"{a} and {b} score below threshold but share a cluster"
        return None

    def is_terminal(self, node: ATSNode) -> bool:
        return sum(len(c) for c in node.state["clusters"]) == len(self.members)

    def state_key(self, node: ATSNode) -> Hashable:
        return tuple(tuple(c) for c in sorted(node.state["clusters"]))


def as_decimal(v) -> Decimal | None:
    try:
        return Decimal(str(v))
    except Exception:
        return None


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
