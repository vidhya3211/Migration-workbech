"""Deterministic PL/SQL reader.

Tokenises PL/SQL (comments/strings aware, line numbers kept) and walks package bodies, functions and
procedures, tracking the IF/ELSIF/ELSE, CASE and LOOP stack so each assignment / RETURN is reported with
the conditions under which it executes. Declarations with ``CONSTANT`` become constant rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class QTok:
    kind: str   # word | num | str | op
    text: str
    line: int

    @property
    def up(self) -> str:
        return self.text.upper()


_TOKEN = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<comment>--[^\n]*|/\*.*?\*/)
    | (?P<str>'(?:[^']|'')*')
    | (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)
    | (?P<word>[A-Za-z_][A-Za-z0-9_$#]*(?:\.[A-Za-z_][A-Za-z0-9_$#]*)*(?:%(?:TYPE|ROWTYPE))?)
    | (?P<op>:=|=>|<>|!=|<=|>=|\|\||\*\*|[-+*/=<>(),;:%.])
    | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)

MATH_FUNCS = {"ROUND", "TRUNC", "MOD", "ABS", "CEIL", "FLOOR", "POWER", "GREATEST", "LEAST", "NVL", "SQRT"}
KEYWORDS = {
    "AND", "OR", "NOT", "NULL", "IS", "IN", "BETWEEN", "LIKE", "TRUE", "FALSE", "THEN", "ELSE", "END", "CASE", "WHEN",
    "SELECT", "INTO", "FROM", "WHERE", "RETURN",
} | MATH_FUNCS


def tokenize(text: str) -> list[QTok]:
    out: list[QTok] = []
    line = 1
    for m in _TOKEN.finditer(text):
        kind = m.lastgroup
        value = m.group()
        if kind not in ("ws", "comment", "other"):
            out.append(QTok(kind, value, line))
        line += value.count("\n")
    return out


def render(toks: list[QTok]) -> str:
    out = ""
    prev: QTok | None = None
    for tk in toks:
        s = tk.text
        if prev is None:
            out = s
        elif s in (")", ",", ";", "%") or prev.text == "(" or (s == "(" and prev.kind == "word"):
            out += s
        else:
            out += " " + s
        prev = tk
    return out


def has_arithmetic(toks: list[QTok]) -> bool:
    for k, tk in enumerate(toks):
        if tk.kind == "op" and tk.text in ("*", "/", "**"):
            return True
        if tk.kind == "op" and tk.text in ("+", "-") and k > 0 and (toks[k - 1].kind in ("word", "num") or toks[k - 1].text == ")"):
            return True
        if tk.kind == "word" and tk.up in MATH_FUNCS:
            return True
    return False


def is_literal(toks: list[QTok]) -> bool:
    if len(toks) == 1:
        return toks[0].kind in ("num", "str") or toks[0].up in ("NULL", "TRUE", "FALSE")
    return len(toks) == 2 and toks[0].text == "-" and toks[1].kind == "num"


def inputs_of(toks: list[QTok]) -> list[str]:
    out: list[str] = []
    for k, tk in enumerate(toks):
        if tk.kind != "word" or tk.up in KEYWORDS:
            continue
        if k + 1 < len(toks) and toks[k + 1].text == "(" and tk.up in MATH_FUNCS:
            continue
        if tk.text not in out:
            out.append(tk.text)
    return out


@dataclass
class Frame:
    kind: str                 # unit | routine | block | if | case | loop
    name: str | None = None
    line: int = 0
    conds: list[str] = field(default_factory=list)
    chain: list[str] = field(default_factory=list)
    in_decl: bool = False
    subject: str | None = None
    text: str | None = None


@dataclass
class PlsqlFinding:
    kind: str                 # calculation | assignment | constant
    target: str
    expression: str
    expr_toks: list[QTok]
    conditions: list[str]
    loops: list[str]
    start_line: int
    end_line: int
    unit: str | None
    routine: str | None
    anchor: str
    confidence: float
    reasons: list[str]
    is_return: bool = False


class PlsqlParser:
    def __init__(self, toks: list[QTok]) -> None:
        self.t = toks
        self.n = len(toks)
        self.frames: list[Frame] = []
        self.findings: list[PlsqlFinding] = []
        self.routines: list[str] = []
        self.tables: set[str] = set()
        self.units: list[str] = []
        self.skipped: list[tuple[int, str]] = []
        self.aliases: dict[str, str] = {}   # local variable -> column it was SELECTed INTO from

    def _tok(self, i: int) -> QTok | None:
        return self.t[i] if 0 <= i < self.n else None

    def _unit(self) -> str | None:
        return next((f.name for f in reversed(self.frames) if f.kind == "unit"), None)

    def _routine(self) -> Frame | None:
        return next((f for f in reversed(self.frames) if f.kind == "routine"), None)

    def _until(self, i: int, stops: set[str]) -> tuple[list[QTok], int]:
        """Collect tokens until a stop keyword/op at paren depth 0 (stop token not consumed)."""
        depth, out = 0, []
        while i < self.n:
            tk = self.t[i]
            if tk.text == "(":
                depth += 1
            elif tk.text == ")":
                depth -= 1
            elif depth == 0 and (tk.up in stops or tk.text in stops):
                break
            out.append(tk)
            i += 1
        return out, i

    def _context(self) -> tuple[list[str], list[str]]:
        conds, loops = [], []
        for f in self.frames:
            if f.kind in ("if", "case"):
                conds.extend(f.conds)
            elif f.kind == "loop":
                loops.append(f"{f.text} (line {f.line})")
        return conds, loops

    def _add(self, kind: str, target: str, expr_toks: list[QTok], start: int, end: int, anchor: str,
             conf: float, reasons: list[str], *, is_return: bool = False, context_free: bool = False) -> None:
        conds, loops = ([], []) if context_free else self._context()
        routine = self._routine()
        self.findings.append(PlsqlFinding(
            kind=kind, target=target, expression=render(expr_toks), expr_toks=expr_toks, conditions=conds,
            loops=loops, start_line=start, end_line=end, unit=self._unit(), routine=routine.name if routine else None,
            anchor=anchor, confidence=conf, reasons=reasons, is_return=is_return,
        ))

    def run(self) -> "PlsqlParser":
        i = 0
        while i < self.n:
            tk = self.t[i]
            u = tk.up
            if tk.text in ("/", ";"):   # SQL*Plus terminator / stray separator
                i += 1
                continue
            if u == "CREATE":
                hdr, i = self._until(i, {"IS", "AS", ";"})
                words = [x.up for x in hdr]
                name_idx = next((k for k, w in enumerate(words) if w in ("BODY", "PACKAGE", "PROCEDURE", "FUNCTION")
                                 and k + 1 < len(hdr) and hdr[k + 1].up != "BODY"), None)
                name = hdr[name_idx + 1].text if name_idx is not None else "anonymous"
                kind = "routine" if "PROCEDURE" in words[:4] or "FUNCTION" in words[:4] else "unit"
                if self._tok(i) is not None and self.t[i].up in ("IS", "AS"):
                    i += 1
                    frame = Frame(kind, name, tk.line, in_decl=True)
                    self.frames.append(frame)
                    (self.routines if kind == "routine" else self.units).append(name)
                else:
                    i += 1
                continue
            if u in ("FUNCTION", "PROCEDURE"):
                hdr, i = self._until(i, {"IS", "AS", ";"})
                name = hdr[1].text if len(hdr) > 1 else "anonymous"
                nxt = self._tok(i)
                if nxt is not None and nxt.up in ("IS", "AS"):
                    self.frames.append(Frame("routine", name, tk.line, in_decl=True))
                    self.routines.append(f"{self._unit()}.{name}" if self._unit() else name)
                i += 1
                continue
            if u == "BEGIN":
                top = self.frames[-1] if self.frames else None
                if top is not None and top.kind in ("routine", "unit") and top.in_decl:
                    top.in_decl = False
                else:
                    self.frames.append(Frame("block", None, tk.line))
                i += 1
                continue
            if u == "IF":
                cond, i = self._until(i + 1, {"THEN"})
                text = render(cond)
                self.frames.append(Frame("if", None, tk.line, conds=[text], chain=[text]))
                i += 1
                continue
            if u == "ELSIF":
                top = self._pop_to("if")
                chain = top.chain if top else []
                cond, i = self._until(i + 1, {"THEN"})
                text = render(cond)
                self.frames.append(Frame("if", None, tk.line, conds=[f"NOT ({c})" for c in chain] + [text],
                                         chain=chain + [text]))
                i += 1
                continue
            if u == "ELSE":
                top = self.frames[-1] if self.frames else None
                if top is not None and top.kind == "if":
                    top.conds = [f"NOT ({c})" for c in top.chain] or ["else"]
                elif top is not None and top.kind == "case":
                    top.conds = [f"NOT ({' OR '.join(top.chain)})"] if top.chain else ["else"]
                i += 1
                continue
            if u == "CASE":   # statement-level CASE
                subj, i = self._until(i + 1, {"WHEN"})
                self.frames.append(Frame("case", None, tk.line, subject=render(subj) or None))
                continue
            if u == "WHEN" and self.frames and self.frames[-1].kind == "case":
                cond, i = self._until(i + 1, {"THEN"})
                top = self.frames[-1]
                text = render(cond)
                c = f"{top.subject} = {text}" if top.subject else text
                top.conds = [c]
                top.chain.append(c)
                i += 1
                continue
            if u in ("LOOP", "FOR", "WHILE"):
                hdr, i = self._until(i, {"LOOP"}) if u != "LOOP" else ([], i)
                self.frames.append(Frame("loop", None, tk.line, text=render(hdr) or "LOOP"))
                i += 1
                continue
            if u == "END":
                nxt = self._tok(i + 1)
                target = {"IF": "if", "LOOP": "loop", "CASE": "case"}.get(nxt.up if nxt else "")
                if target:
                    self._pop_to(target)
                    i += 2
                elif self.frames:
                    self.frames.pop()
                    i += 1
                while i < self.n and self.t[i].text != ";":
                    i += 1
                i += 1
                continue
            if u in ("EXCEPTION",):
                i += 1
                continue
            i = self._statement(i)
        return self

    def _pop_to(self, kind: str) -> Frame | None:
        while self.frames:
            f = self.frames.pop()
            if f.kind == kind:
                return f
        return None

    def _statement(self, i: int) -> int:
        start = self.t[i]
        toks, j = self._until(i, {";"})
        j += 1
        if not toks:
            return j
        routine = self._routine()
        top = self.frames[-1] if self.frames else None
        in_decl = top is not None and top.kind in ("routine", "unit") and top.in_decl
        words = [x.up for x in toks]
        end_line = max(x.line for x in toks)
        if "SELECT" in words[:1] or "UPDATE" in words[:1] or "INSERT" in words[:1] or "DELETE" in words[:1]:
            if words[0] == "SELECT" and "INTO" in words and "FROM" in words:
                into, frm = words.index("INTO"), words.index("FROM")
                cols = [x.text for x in toks[1:into] if x.kind == "word"]
                targets = [x.text for x in toks[into + 1:frm] if x.kind == "word"]
                if len(cols) == len(targets):   # a column alias beats a routine-local scope name
                    self.aliases.update({t.upper(): c for t, c in zip(targets, cols)})
            for k, w in enumerate(words):
                if w in ("FROM", "UPDATE", "INTO", "JOIN") and k + 1 < len(toks) and toks[k + 1].kind == "word" \
                        and not toks[k + 1].text.lower().startswith(("v_", "p_", "l_")):
                    self.tables.add(toks[k + 1].up)
            return j
        if in_decl:
            if top is not None and top.kind == "routine" and toks[0].kind == "word":
                local = re.sub(r"^[vlp]_", "", toks[0].text, flags=re.I)
                self.aliases.setdefault(toks[0].up, f"{top.name}_{local}")   # routine-scoped local
            assign = next((k for k, x in enumerate(toks) if x.text in (":=",) or x.up == "DEFAULT"), None)
            if assign is not None and toks[0].kind == "word":
                rhs = toks[assign + 1:]
                if "CONSTANT" in words:
                    self._add("constant", toks[0].text, rhs, start.line, end_line, toks[0].text, 0.95,
                              ["CONSTANT declaration"], context_free=True)
                elif is_literal(rhs) and render(rhs).upper() not in ("0", "NULL"):
                    self._add("constant", toks[0].text, rhs, start.line, end_line, toks[0].text, 0.6,
                              ["variable default value; may be reassigned"], context_free=True)
            return j
        if words[0] == "RETURN" and routine is not None and len(toks) > 1:
            rhs = toks[1:]
            conds = self._context()[0]
            if has_arithmetic(rhs):
                self._add("calculation", f"{routine.name}()", rhs, start.line, end_line, "RETURN", 0.85,
                          ["deterministic parse of arithmetic RETURN"], is_return=True)
            elif is_literal(rhs) and (conds or render(rhs) not in ("0", "NULL")):
                self._add("assignment", f"{routine.name}()", rhs, start.line, end_line, "RETURN", 0.8,
                          ["literal RETURN value"], is_return=True)
            elif conds:
                self._add("assignment", f"{routine.name}()", rhs, start.line, end_line, "RETURN", 0.6,
                          ["conditional RETURN of a variable"], is_return=True)
            return j
        assign = next((k for k, x in enumerate(toks) if x.text == ":="), None)
        if assign is not None and assign > 0:
            lhs, rhs = toks[:assign], toks[assign + 1:]
            target = render(lhs)
            conds = self._context()[0]
            if has_arithmetic(rhs):
                self._add("calculation", target, rhs, start.line, end_line, lhs[0].text, 0.85,
                          ["deterministic parse of arithmetic assignment"])
            elif is_literal(rhs) and (conds or render(rhs).upper() not in ("0", "NULL")):
                self._add("assignment", target, rhs, start.line, end_line, lhs[0].text, 0.8, ["literal assignment"])
            elif conds:
                self._add("assignment", target, rhs, start.line, end_line, lhs[0].text, 0.6,
                          ["conditional data move (no literal)"])
            return j
        if routine is None and words[0] not in ("NULL",):
            self.skipped.append((start.line, f"statement outside a routine: {render(toks[:6])}"))
        return j
