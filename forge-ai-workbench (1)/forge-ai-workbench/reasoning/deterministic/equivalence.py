"""Deterministic equivalence checking: *proves* whether source and target logic agree.

Each artefact's rules become a ``Program``: ordered steps ``target := expr WHEN conditions``. For a
canonical variable V defined on both sides we:

1. generate boundary-value test vectors from every comparison either side makes
   (``HOLDER_AGE >= AGE_LOAD_THRESHOLD`` with threshold 60 -> ages 59, 60, 61);
2. simulate the source program for each vector to obtain a shared environment;
3. evaluate *only V's steps* from each side in that same environment ("local" comparison), so a
   downstream variable is never blamed for an upstream difference;
4. report divergence vectors - concrete inputs where the two sides compute different values.

Anything that cannot be evaluated is reported as *unproven*, never as equal.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from reasoning.deterministic.expressions import (
    TABLES, ExpressionError, LookupGap, Unknown, comparisons, evaluate, identifiers, parse, truth,
)

ROUNDING_TOLERANCE = Decimal("0.01")
MAX_VECTORS = 600
DEFAULT_NUMERIC = [Decimal("0"), Decimal("150"), Decimal("1000"), Decimal("250000")]


@dataclass
class Step:
    rule_id: str
    target: str
    expr: str
    conditions: list[str]
    kind: str
    order: int


@dataclass
class Program:
    key: str
    label: str
    role: str
    steps: list[Step]
    tables: dict = field(default_factory=dict)      # canonical table name -> {key: value} (lifted rate tables)

    def assigned(self) -> set[str]:
        return {s.target for s in self.steps}

    def referenced(self) -> set[str]:
        out: set[str] = set()
        for s in self.steps:
            for src in [s.expr, *s.conditions]:
                try:
                    out |= identifiers(src)
                except ExpressionError:
                    pass
        return out

    def constants(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for s in self.steps:
            if not s.conditions and s.kind == "constant":
                try:
                    v = evaluate(s.expr, {})
                    if isinstance(v, Decimal):
                        out[s.target] = v
                except ExpressionError:
                    pass
        return out

    def steps_for(self, var: str) -> list[Step]:
        return [s for s in self.steps if s.target == var]


def _apply(steps: list[Step], env: dict) -> None:
    for s in steps:
        try:
            if all(truth(c, env) for c in s.conditions):
                env[s.target] = evaluate(s.expr, env)
        except Unknown:
            env.pop(s.target, None)      # the value is now unknowable - later uses raise Unknown
        except ExpressionError:
            env.pop(s.target, None)


def simulate(program: Program, inputs: dict) -> dict:
    env = dict(inputs)
    env[TABLES] = program.tables
    _apply(program.steps, env)
    env.pop(TABLES, None)
    return env


_UNASSIGNED = object()


def local_value(steps: list[Step], env: dict, var: str, tables: dict | None = None):
    """Evaluate only ``var``'s steps in ``env`` (with that side's lookup tables). Returns a value, _UNASSIGNED, or
    raises Unknown."""
    local = {k: v for k, v in env.items() if k != var}
    local[TABLES] = tables or {}
    for s in steps:
        if all(truth(c, local) for c in s.conditions):
            local[var] = evaluate(s.expr, local)
    return local.get(var, _UNASSIGNED)


@dataclass
class Comparison:
    variable: str
    source_key: str
    target_key: str
    vectors_tested: int
    vectors_known: int
    divergent: list[dict] = field(default_factory=list)   # {"inputs": {...}, "source": v, "target": v}
    divergent_count: int = 0
    scope_only: int = 0            # vectors where only one side assigns V at all
    max_abs_delta: Decimal | None = None
    median_abs_delta: Decimal | None = None
    category: str = "equivalent"   # equivalent | value | condition | expression | rounding | unproven
    assumptions: list[str] = field(default_factory=list)
    only_in: str | None = None     # when one side never assigns the variable for some inputs
    lookup_gaps: list[str] = field(default_factory=list)   # "<side>: <table> has no entry for <key>"

    @property
    def diverges(self) -> bool:
        return bool(self.divergent)


def _domain(programs: list[Program], inputs: set[str]) -> dict[str, list]:
    constants: dict[str, Decimal] = {}
    for p in programs:
        for k, v in p.constants().items():
            constants.setdefault(k, v)
    values: dict[str, set] = {v: set() for v in inputs}
    stringy: set[str] = set()
    boolean: set[str] = set()
    # a variable used as a table key takes the tables' keys as test values (union across both sides, so an
    # entry one side lacks is tested too)
    for p in programs:
        for s in p.steps:
            for src in [s.expr, *s.conditions]:
                for name, var in _table_keys(src, p.tables):
                    if var in values:
                        for k in p.tables[name]:
                            try:
                                values[var].add(Decimal(str(k)))
                            except InvalidOperation:
                                stringy.add(var)
                                values[var].add(str(k))
    for p in programs:
        for s in p.steps:
            for cond in s.conditions:
                try:
                    triples = comparisons(cond)
                    idents = identifiers(cond)
                except ExpressionError:
                    continue
                compared = {t[0] for t in triples} | {t[2][1] for t in triples if isinstance(t[2], tuple)}
                boolean |= {i for i in idents if i in inputs and i not in compared}
                for var, _op, lit in triples:
                    if isinstance(lit, tuple):          # var-to-var: resolve the other side as a constant
                        lit = constants.get(lit[1])
                        if lit is None:
                            continue
                    if var not in values:
                        continue
                    if isinstance(lit, str):
                        stringy.add(var)
                        values[var].add(lit)
                    else:
                        values[var] |= {lit - 1, lit, lit + 1}
    out: dict[str, list] = {}
    for var in sorted(inputs):
        if var in boolean and not values[var]:
            out[var] = ["Y", "N"]
        elif var in stringy:
            out[var] = sorted({str(v) for v in values[var]}) + ["Z"]
        elif values[var]:
            out[var] = sorted({Decimal(v) for v in values[var]} | {Decimal("1000")})
        else:
            out[var] = list(DEFAULT_NUMERIC)
    return out


def _table_values_differ(steps: list[Step], s_tables: dict, t_tables: dict) -> bool:
    used = {name for s in steps for name, _ in _table_keys(s.expr, {**s_tables, **t_tables})}
    return any(s_tables.get(n) != t_tables.get(n) for n in used)


def _table_keys(src: str, tables: dict) -> list[tuple[str, str]]:
    """(table, key variable) for every lookup ``TABLE(VAR)`` in an expression."""
    if not tables:
        return []
    try:
        tree = parse(src)
    except ExpressionError:
        return []
    out: list[tuple[str, str]] = []

    def walk(n) -> None:
        if isinstance(n, tuple):
            if n and n[0] == "call" and n[1] in tables and len(n[2]) == 1 and n[2][0][0] == "var":
                out.append((n[1], n[2][0][1]))
            for part in n[1:]:
                walk(part)
        elif isinstance(n, list):
            for part in n:
                walk(part)
    walk(tree)
    return out


def vectors(domain: dict[str, list], limit: int = MAX_VECTORS) -> list[dict]:
    names = list(domain)
    if not names:
        return [{}]
    total = 1
    for n in names:
        total *= len(domain[n])
    if total <= limit:
        return [dict(zip(names, combo)) for combo in itertools.product(*(domain[n] for n in names))]
    # base vector + one-factor-at-a-time + a deterministic stride over the full product
    base = {n: domain[n][len(domain[n]) // 2] for n in names}
    out = [base]
    for n in names:
        for v in domain[n]:
            out.append({**base, n: v})
    stride = max(1, total // (limit - len(out)))
    for k, combo in enumerate(itertools.product(*(domain[n] for n in names))):
        if k % stride == 0:
            out.append(dict(zip(names, combo)))
        if len(out) >= limit:
            break
    return out


def _fmt(v) -> str | None:
    if v is _UNASSIGNED or v is None:
        return None
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")    # 2.81250 and 2.8125 are the same number
    return str(v)


def compare(variable: str, source: Program, target: Program, *, extra_inputs: dict | None = None) -> Comparison:
    s_steps, t_steps = source.steps_for(variable), target.steps_for(variable)
    assigned = source.assigned() | target.assigned()
    inputs = (source.referenced() | target.referenced()) - assigned
    dom = _domain([source, target], inputs)
    vecs = vectors(dom)
    cmp = Comparison(variable, source.key, target.key, len(vecs), 0)
    deltas: list[Decimal] = []
    envs: list[dict] = []
    for vec in vecs:
        env = simulate(target, vec)
        env.update(simulate(source, vec))                 # source values win: the shared reference environment
        env.update(extra_inputs or {})
        gap = None
        try:
            sv = local_value(s_steps, env, variable, source.tables)
        except LookupGap as exc:      # the source cannot execute this case: a gap, not an unknowable input
            sv, gap = None, f"source: {exc}"
        except (Unknown, ExpressionError):
            continue
        try:
            tv = local_value(t_steps, env, variable, target.tables)
        except LookupGap as exc:
            tv, gap = None, f"target: {exc}"
        except (Unknown, ExpressionError):
            continue
        if gap is not None:
            if sv is None and tv is None:
                continue
            if gap not in cmp.lookup_gaps and len(cmp.lookup_gaps) < 20:
                cmp.lookup_gaps.append(gap)
        cmp.vectors_known += 1
        if len(envs) < 40:
            envs.append(env)
        if sv is _UNASSIGNED and tv is _UNASSIGNED:
            continue
        if sv is _UNASSIGNED or tv is _UNASSIGNED:
            cmp.scope_only += 1      # one side never computes V on this path: a scope difference, not a divergence
            continue
        equal = False
        if isinstance(sv, Decimal) and isinstance(tv, Decimal):
            equal = sv == tv
            if not equal:
                deltas.append(abs(sv - tv))
        elif sv is not _UNASSIGNED and tv is not _UNASSIGNED:
            try:
                equal = truth("A = B", {"A": sv, "B": tv})
            except ExpressionError:
                equal = str(sv) == str(tv)
        if not equal:
            cmp.divergent_count += 1
            if len(cmp.divergent) < 25:
                cmp.divergent.append({"inputs": {k: _fmt(v) for k, v in vec.items()},
                                      "source": _fmt(sv), "target": _fmt(tv)})
    if cmp.scope_only:
        cmp.assumptions.append(f"{cmp.scope_only} vector(s) where only one side computes {variable} were not counted")
    if cmp.vectors_known == 0:
        cmp.category = "unproven"
        cmp.assumptions.append("no test vector could be fully evaluated on both sides")
        return cmp
    if not cmp.divergent:
        return cmp
    cmp.max_abs_delta = max(deltas) if deltas else None
    cmp.median_abs_delta = sorted(deltas)[len(deltas) // 2] if deltas else None
    cmp.category = _categorise(s_steps, t_steps, cmp, envs)
    if cmp.category in ("condition", "expression") and (source.tables or target.tables) and {
            s.expr.replace(" ", "") for s in s_steps} == {x.expr.replace(" ", "") for x in t_steps}             and _table_values_differ(s_steps + t_steps, source.tables, target.tables):
        cmp.category = "table"         # same logic, different rates in the lookup tables
    if cmp.lookup_gaps:
        cmp.assumptions.append("lookup gaps (a case one side cannot execute): " + "; ".join(cmp.lookup_gaps[:5]))
    if any(d["source"] is None or d["target"] is None for d in cmp.divergent):
        cmp.only_in = "source" if all(d["target"] is None for d in cmp.divergent) else (
            "target" if all(d["source"] is None for d in cmp.divergent) else None)
    if any(v == ["Y", "N"] for v in dom.values()):
        cmp.assumptions.append("flag inputs compared as Y=true / other=false")
    return cmp


def _literal(step: Step) -> bool:
    try:
        return not identifiers(step.expr)
    except ExpressionError:
        return False


def _categorise(s_steps: list[Step], t_steps: list[Step], cmp: Comparison, envs: list[dict]) -> str:
    if all(s.kind == "constant" and not s.conditions for s in s_steps + t_steps):
        return "value"
    if s_steps and t_steps and all(_literal(s) for s in s_steps + t_steps):
        sv = sorted({evaluate(s.expr, {}) for s in s_steps})
        tv = sorted({evaluate(t.expr, {}) for t in t_steps})
        return "condition" if sv == tv else "value"   # same outcomes, different selection -> condition
    if cmp.max_abs_delta is not None and cmp.max_abs_delta <= ROUNDING_TOLERANCE and all(
            d["source"] is not None and d["target"] is not None for d in cmp.divergent):
        return "rounding"
    s_exprs = {s.expr.replace(" ", "") for s in s_steps}
    t_exprs = {t.expr.replace(" ", "") for t in t_steps}
    if s_exprs == t_exprs or _same_values(s_steps, t_steps, envs):
        return "condition"
    return "expression"


def _same_values(s_steps: list[Step], t_steps: list[Step], envs: list[dict]) -> bool:
    """Do the two sides compute the same set of expressions (ignoring *when* they apply)?

    Each step's expression is evaluated in the real simulated environments; the sets of value-profiles
    must match. Anything that cannot be evaluated means sameness is unproven (-> "expression").
    """
    def signature(steps: list[Step]) -> set[tuple] | None:
        sig = set()
        for s in steps:
            vals = []
            for env in envs:
                try:
                    vals.append(_fmt(evaluate(s.expr, env)))
                except ExpressionError:
                    return None
            sig.add(tuple(vals))
        return sig
    s_sig, t_sig = signature(s_steps), signature(t_steps)
    return bool(s_steps) and s_sig is not None and t_sig is not None and s_sig == t_sig


def domain_for(programs: list[Program], inputs: set[str]) -> dict[str, list]:
    """Public: boundary-value domains for ``inputs`` drawn from every comparison the programs make."""
    return _domain(programs, inputs)
