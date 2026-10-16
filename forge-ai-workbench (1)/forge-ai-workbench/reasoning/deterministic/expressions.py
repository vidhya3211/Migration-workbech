"""Safe, exact (Decimal) evaluator for canonical expressions and conditions.

Canonical syntax (produced by ``canonical.py`` from COBOL / PHP / PL/SQL / Excel):
    identifiers  UPPER_SNAKE           numbers 12.50        strings 'A'
    arithmetic   + - * / **            functions ROUND(x, n) TRUNC(x, n) ABS MIN MAX
    comparison   = != < <= > >= (chainable: 1 <= X <= 10)    X IN ('P', 'R')
    logic        AND OR NOT            CASE WHEN c THEN v [WHEN ...] [ELSE v] END

No ``eval``: a hand-written recursive-descent parser. Missing identifiers raise ``Unknown`` so callers
can distinguish "cannot be proved" from "proved different".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, DivisionByZero, InvalidOperation, localcontext
from typing import Any

Value = Decimal | str | bool | None


class ExpressionError(ValueError):
    pass


class Unknown(ExpressionError):
    """An identifier has no value in this environment - the result cannot be proved."""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown identifier {name}")
        self.name = name


_TOKEN = re.compile(r"""
    (?P<ws>\s+)|(?P<num>\d+(?:\.\d+)?|\.\d+)|(?P<str>'(?:[^']|'')*')|
    (?P<ident>[A-Za-z_][A-Za-z0-9_]*)|(?P<op>\*\*|<=|>=|!=|<>|==|[-+*/()=<>,])|(?P<bad>.)""", re.X)
_KW = {"AND", "OR", "NOT", "IN", "CASE", "WHEN", "THEN", "ELSE", "END", "TRUE", "FALSE", "NULL"}


@dataclass
class Tok:
    kind: str
    text: str


def tokenize(src: str) -> list[Tok]:
    out = []
    for m in _TOKEN.finditer(src):
        kind = m.lastgroup
        if kind == "ws":
            continue
        if kind == "bad":
            raise ExpressionError(f"unexpected character {m.group()!r} in {src!r}")
        text = m.group()
        if kind == "ident" and text.upper() in _KW:
            kind, text = "kw", text.upper()
        elif kind == "op" and text in ("==", "<>"):
            text = "=" if text == "==" else "!="
        out.append(Tok(kind, text))
    return out


# AST nodes are tuples: ("num", Decimal) ("str", s) ("var", name) ("bin", op, a, b) ("neg", a) ("not", a)
# ("and", a, b) ("or", a, b) ("cmp", [ops], [operands]) ("in", a, [items]) ("call", name, [args]) ("case", [(c, v)], else)
class Parser:
    def __init__(self, toks: list[Tok]) -> None:
        self.t = toks
        self.i = 0

    def peek(self, text: str | None = None, kind: str | None = None) -> bool:
        if self.i >= len(self.t):
            return False
        tk = self.t[self.i]
        return (text is None or tk.text == text) and (kind is None or tk.kind == kind)

    def take(self, text: str | None = None) -> Tok:
        if self.i >= len(self.t) or (text is not None and self.t[self.i].text != text):
            got = self.t[self.i].text if self.i < len(self.t) else "end of input"
            raise ExpressionError(f"expected {text!r}, got {got!r}")
        self.i += 1
        return self.t[self.i - 1]

    def parse(self) -> tuple:
        node = self.or_()
        if self.i != len(self.t):
            raise ExpressionError(f"unexpected {self.t[self.i].text!r}")
        return node

    def or_(self) -> tuple:
        node = self.and_()
        while self.peek("OR"):
            self.take()
            node = ("or", node, self.and_())
        return node

    def and_(self) -> tuple:
        node = self.not_()
        while self.peek("AND"):
            self.take()
            node = ("and", node, self.not_())
        return node

    def not_(self) -> tuple:
        if self.peek("NOT"):
            self.take()
            return ("not", self.not_())
        return self.cmp()

    def cmp(self) -> tuple:
        first = self.add()
        if self.peek("NOT") and self.i + 1 < len(self.t) and self.t[self.i + 1].text == "IN":
            self.take()
            return ("not", self._in(first))
        if self.peek("IN"):
            return self._in(first)
        ops, operands = [], [first]
        while self.i < len(self.t) and self.t[self.i].text in ("=", "!=", "<", "<=", ">", ">="):
            ops.append(self.take().text)
            operands.append(self.add())
        return ("cmp", ops, operands) if ops else first

    def _in(self, left: tuple) -> tuple:
        self.take("IN")
        self.take("(")
        items = [self.add()]
        while self.peek(","):
            self.take()
            items.append(self.add())
        self.take(")")
        return ("in", left, items)

    def add(self) -> tuple:
        node = self.mul()
        while self.peek("+") or self.peek("-"):
            op = self.take().text
            node = ("bin", op, node, self.mul())
        return node

    def mul(self) -> tuple:
        node = self.unary()
        while self.peek("*") or self.peek("/"):
            op = self.take().text
            node = ("bin", op, node, self.unary())
        return node

    def unary(self) -> tuple:
        if self.peek("-"):
            self.take()
            return ("neg", self.unary())
        if self.peek("+"):
            self.take()
            return self.unary()
        return self.power()

    def power(self) -> tuple:
        node = self.atom()
        if self.peek("**"):
            self.take()
            return ("bin", "**", node, self.unary())
        return node

    def atom(self) -> tuple:
        if self.i >= len(self.t):
            raise ExpressionError("unexpected end of expression")
        tk = self.take()
        if tk.kind == "num":
            return ("num", Decimal(tk.text))
        if tk.kind == "str":
            return ("str", tk.text[1:-1].replace("''", "'"))
        if tk.text == "(":
            node = self.or_()
            self.take(")")
            return node
        if tk.text in ("TRUE", "FALSE"):
            return ("bool", tk.text == "TRUE")
        if tk.text == "NULL":
            return ("null",)
        if tk.text == "CASE":
            branches, default = [], None
            while self.peek("WHEN"):
                self.take()
                cond = self.or_()
                self.take("THEN")
                branches.append((cond, self.or_()))
            if self.peek("ELSE"):
                self.take()
                default = self.or_()
            self.take("END")
            return ("case", branches, default)
        if tk.kind == "ident":
            if self.peek("("):
                self.take()
                args = []
                if not self.peek(")"):
                    args.append(self.or_())
                    while self.peek(","):
                        self.take()
                        args.append(self.or_())
                self.take(")")
                return ("call", tk.text.upper(), args)
            return ("var", tk.text.upper())
        raise ExpressionError(f"unexpected token {tk.text!r}")


def parse(src: str) -> tuple:
    return Parser(tokenize(src)).parse()


def _num(v: Value) -> Decimal:
    if isinstance(v, bool):
        return Decimal(int(v))
    if isinstance(v, Decimal):
        return v
    if v is None:
        raise ExpressionError("arithmetic on NULL")
    try:
        return Decimal(str(v))
    except InvalidOperation:
        raise ExpressionError(f"not a number: {v!r}") from None


def _eq(a: Value, b: Value) -> bool:
    if isinstance(a, str) or isinstance(b, str):
        return str(a).strip() == str(b).strip()
    if a is None or b is None:
        return a is b
    return _num(a) == _num(b)


TRUE_FLAGS = frozenset({"Y", "YES", "TRUE", "T", "1"})


def _truthy(v: Value) -> bool:
    """Flag semantics are explicit: a legacy 'Y'/'N' flag and a boolean compare the same way."""
    if isinstance(v, bool):
        return v
    if isinstance(v, Decimal):
        return v != 0
    if isinstance(v, str):
        return v.strip().upper() in TRUE_FLAGS
    return bool(v)


_CMP = {"=": _eq, "!=": lambda a, b: not _eq(a, b), "<": lambda a, b: _num(a) < _num(b),
        "<=": lambda a, b: _num(a) <= _num(b), ">": lambda a, b: _num(a) > _num(b),
        ">=": lambda a, b: _num(a) >= _num(b)}


def _round(x: Decimal, n: Decimal, mode: str) -> Decimal:
    q = Decimal(1).scaleb(-int(n))
    return x.quantize(q, rounding=mode)


TABLES = "__TABLES__"          # env key (evaluate() upper-cases keys): {TABLE_NAME: {key: value}} - the lookup tables of the running program
BUILTINS = {"ROUND", "TRUNC", "ABS", "MIN", "MAX", "NVL", "COALESCE", "MOD"}


class LookupGap(ExpressionError):
    """A table lookup for a key the table does not hold: the logic cannot execute for this input."""


def _table_key(v: Value) -> str:
    if isinstance(v, Decimal):
        return format(v.normalize(), "f") if v == v.to_integral_value() else format(v.normalize(), "f")
    if isinstance(v, bool) or v is None:
        raise ExpressionError(f"table key {v!r}")
    return str(v).strip().upper()


def _lookup(name: str, table: dict, args: list[Value]) -> Value:
    if len(args) != 1:
        raise ExpressionError(f"{name}() table lookups take one key")
    key = _table_key(args[0])
    entries = {str(k).strip().upper(): v for k, v in table.items()}
    if key not in entries:
        raise LookupGap(f"{name} has no entry for {key}")
    v = entries[key]
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return v


def _call(name: str, args: list[Value]) -> Value:
    if name == "ROUND":
        return _round(_num(args[0]), _num(args[1]) if len(args) > 1 else Decimal(0), ROUND_HALF_UP)
    if name == "TRUNC":
        return _round(_num(args[0]), _num(args[1]) if len(args) > 1 else Decimal(0), ROUND_DOWN)
    if name == "ABS":
        return abs(_num(args[0]))
    if name == "MIN":
        return min(_num(a) for a in args)
    if name == "MAX":
        return max(_num(a) for a in args)
    if name in ("NVL", "COALESCE"):
        return next((a for a in args if a is not None), None)
    if name == "MOD":
        return _num(args[0]) % _num(args[1])
    raise Unknown(f"{name}()")


def eval_node(node: tuple, env: dict[str, Any]) -> Value:
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "str":
        return node[1]
    if kind == "bool":
        return node[1]
    if kind == "null":
        return None
    if kind == "var":
        if node[1] not in env:
            raise Unknown(node[1])
        v = env[node[1]]
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return Decimal(str(v))
        return v
    if kind == "neg":
        return -_num(eval_node(node[1], env))
    if kind == "not":
        return not _truthy(eval_node(node[1], env))
    if kind == "and":
        return _truthy(eval_node(node[1], env)) and _truthy(eval_node(node[2], env))
    if kind == "or":
        return _truthy(eval_node(node[1], env)) or _truthy(eval_node(node[2], env))
    if kind == "bin":
        a, b = _num(eval_node(node[2], env)), _num(eval_node(node[3], env))
        with localcontext() as ctx:
            ctx.prec = 34
            try:
                match node[1]:
                    case "+":
                        return a + b
                    case "-":
                        return a - b
                    case "*":
                        return a * b
                    case "/":
                        return a / b
                    case "**":
                        return a ** b
            except (DivisionByZero, InvalidOperation) as exc:
                raise ExpressionError(f"arithmetic error: {exc!r}") from None
    if kind == "cmp":
        vals = [eval_node(x, env) for x in node[2]]
        return all(_CMP[op](vals[k], vals[k + 1]) for k, op in enumerate(node[1]))
    if kind == "in":
        left = eval_node(node[1], env)
        return any(_eq(left, eval_node(x, env)) for x in node[2])
    if kind == "call":
        args = [eval_node(a, env) for a in node[2]]
        tables = env.get(TABLES) or {}
        if node[1] not in BUILTINS and node[1] in tables:
            return _lookup(node[1], tables[node[1]], args)
        return _call(node[1], args)
    if kind == "case":
        for cond, val in node[1]:
            if _truthy(eval_node(cond, env)):
                return eval_node(val, env)
        return eval_node(node[2], env) if node[2] is not None else None
    raise ExpressionError(f"bad node {kind}")


def evaluate(src: str, env: dict[str, Any] | None = None) -> Value:
    return eval_node(parse(src), {k.upper(): v for k, v in (env or {}).items()})


def truth(src: str, env: dict[str, Any]) -> bool:
    return _truthy(evaluate(src, env))


def identifiers(src: str) -> set[str]:
    out: set[str] = set()

    def walk(n: tuple) -> None:
        if n[0] == "var":
            out.add(n[1])
        for part in n[1:]:
            if isinstance(part, tuple):
                walk(part)
            elif isinstance(part, list):
                for p in part:
                    if isinstance(p, tuple):
                        if p and isinstance(p[0], tuple):
                            for q in p:
                                walk(q)
                        else:
                            walk(p)
    walk(parse(src))
    return out


def comparisons(src: str) -> list[tuple[str, str, Value]]:
    """(identifier, operator, literal) triples used for boundary-value test generation."""
    out: list[tuple[str, str, Value]] = []

    def walk(n: tuple) -> None:
        if n[0] == "cmp":
            ops, xs = n[1], n[2]
            for k, op in enumerate(ops):
                a, b = xs[k], xs[k + 1]
                if a[0] == "var" and b[0] in ("num", "str"):
                    out.append((a[1], op, b[1]))
                elif b[0] == "var" and a[0] in ("num", "str"):
                    out.append((b[1], op, a[1]))
                elif a[0] == "var" and b[0] == "var":
                    out.append((a[1], op, ("var", b[1])))   # resolved against constants later
        if n[0] == "in" and n[1][0] == "var":
            for item in n[2]:
                if item[0] in ("num", "str"):
                    out.append((n[1][1], "=", item[1]))
        for part in n[1:]:
            if isinstance(part, tuple):
                walk(part)
            elif isinstance(part, list):
                for p in part:
                    if isinstance(p, tuple) and p and isinstance(p[0], tuple):
                        for q in p:
                            walk(q)
                    elif isinstance(p, tuple):
                        walk(p)
    walk(parse(src))
    return out
