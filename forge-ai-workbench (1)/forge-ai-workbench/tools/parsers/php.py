"""Deterministic PHP reader for Integro target logic.

Tokenises PHP (comments/strings aware, line numbers kept), then walks statements
tracking class / function scope and the if-elseif-else / switch / loop stack so each
calculation is reported with the conditions under which it runs.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- tokens


@dataclass
class PTok:
    kind: str  # var | ident | num | str | op | attr
    text: str
    line: int


_TOKEN = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<comment>//[^\n]*|\#(?!\[)[^\n]*|/\*.*?\*/)
    | (?P<attr>\#\[)
    | (?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
    | (?P<var>\$[A-Za-z_\x80-￿][\w\x80-￿]*)
    | (?P<num>0[xX][0-9a-fA-F_]+|0[bB][01_]+|\d[\d_]*\.\d[\d_]*(?:[eE][+-]?\d+)?|\.\d[\d_]*(?:[eE][+-]?\d+)?|\d[\d_]*(?:[eE][+-]?\d+)?)
    | (?P<ident>\\?[A-Za-z_\x80-￿][\w\x80-￿]*(?:\\[A-Za-z_][\w]*)*)
    | (?P<op>\?->|<=>|===|!==|\*\*=|\?\?=|\.\.\.|<<=|>>=|==|!=|<>|<=|>=|&&|\|\||\?\?|->|::|=>|\+\+|--|\+=|-=|\*=|/=|\.=|%=|&=|\|=|\^=|\*\*|<<|>>|[-+*/%=<>!.?:;,(){}\[\]&|^~@$])
    | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)


def _php_only(text: str) -> str:
    """Blank inline HTML outside <?php ... ?> (keeping newlines so line numbers hold)."""
    def blank(s: str) -> str:
        return re.sub(r"[^\n]", " ", s)

    out: list[str] = []
    i, inside = 0, False
    while i < len(text):
        if not inside:
            m = re.compile(r"<\?(?:php\b|=)?", re.I).search(text, i)
            if not m:
                out.append(blank(text[i:]))
                break
            out.append(blank(text[i : m.end()]))
            i, inside = m.end(), True
        else:
            j = text.find("?>", i)
            if j == -1:
                out.append(text[i:])
                break
            out.append(text[i:j] + "; ")  # ?> acts as a statement terminator
            i, inside = j + 2, False
    return "".join(out)


def tokenize(text: str) -> list[PTok]:
    src = _php_only(text)
    newlines = [m.start() for m in re.finditer("\n", src)]
    toks: list[PTok] = []
    for m in _TOKEN.finditer(src):
        kind = m.lastgroup
        if kind in ("ws", "comment", "other"):
            continue
        toks.append(PTok(kind, m.group(), bisect.bisect_right(newlines, m.start() - 1) + 1))
    return toks


_NO_SPACE_BEFORE = {")", "]", ",", ";", "->", "?->", "::", "++", "--"}
_NO_SPACE_AFTER = {"(", "[", "->", "?->", "::", "!", "@"}


def render(toks: list[PTok]) -> str:
    out = ""
    prev: PTok | None = None
    prev2: PTok | None = None
    for tk in toks:
        s = tk.text
        call = s in ("(", "[") and prev is not None and (prev.kind in ("ident", "var") or prev.text in (")", "]"))
        unary = (
            prev is not None and prev.text == "-" and tk.kind in ("num", "var")
            and (prev2 is None or (prev2.kind == "op" and prev2.text not in (")", "]")) or prev2.text == "return")
        )
        if prev is None:
            out = s
        elif s in _NO_SPACE_BEFORE or prev.text in _NO_SPACE_AFTER or call or unary:
            out += s
        else:
            out += " " + s
        prev2, prev = prev, tk
    return out


ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", ".=", "%=", "**=", "??="}
ARITH_ASSIGN = {"+=": "+", "-=": "-", "*=": "*", "/=": "/", "%=": "%", "**=": "**"}
MATH_FUNCS = {
    "round", "floor", "ceil", "min", "max", "abs", "pow", "intdiv", "fmod", "sqrt", "bcadd", "bcsub", "bcmul",
    "bcdiv", "bcmod", "bcpow", "number_format", "array_sum", "array_product",
}
TRIVIAL_LITERALS = {"0", "0.0", "0.00", "''", '""', "null", "false", "true", "[]", "1"}


def has_arithmetic(toks: list[PTok]) -> bool:
    prev: PTok | None = None
    for k, tk in enumerate(toks):
        if tk.kind == "op" and tk.text in ("*", "/", "%", "**"):
            return True
        if tk.kind == "op" and tk.text in ("+", "-") and prev is not None and (
            prev.kind in ("var", "num", "str", "ident") or prev.text in (")", "]")
        ):
            return True
        if tk.kind == "ident" and tk.text.lower() in MATH_FUNCS and k + 1 < len(toks) and toks[k + 1].text == "(":
            return True
        prev = tk
    return False


def has_conditional(toks: list[PTok]) -> bool:
    return any(tk.text in ("?", "??") or (tk.kind == "ident" and tk.text.lower() == "match") for tk in toks)


def is_literal(toks: list[PTok]) -> bool:
    if len(toks) == 1:
        return toks[0].kind in ("num", "str") or toks[0].text.lower() in ("true", "false", "null")
    if len(toks) == 2:
        return (toks[0].text == "-" and toks[1].kind == "num") or (toks[0].text == "[" and toks[1].text == "]")
    return False


def is_constant_ref(toks: list[PTok]) -> bool:
    if len(toks) == 1 and toks[0].kind == "ident":
        return toks[0].text.isupper()
    return len(toks) == 3 and toks[0].kind == "ident" and toks[1].text == "::" and toks[2].kind == "ident"


def inputs_of(toks: list[PTok]) -> list[str]:
    out: list[str] = []
    k = 0
    while k < len(toks):
        tk = toks[k]
        if tk.kind == "var":
            chain, j = tk.text, k + 1
            while j + 1 < len(toks) and toks[j].text in ("->", "?->") and toks[j + 1].kind == "ident":
                chain += "->" + toks[j + 1].text
                j += 2
                if j < len(toks) and toks[j].text == "(":
                    chain += "()"
                    break
            if chain != "$this" and chain not in out:
                out.append(chain)
            k = j
            continue
        if tk.kind == "ident" and k + 2 < len(toks) and toks[k + 1].text == "::" and toks[k + 2].kind in ("ident", "var"):
            is_call = k + 3 < len(toks) and toks[k + 3].text == "("
            name = f"{tk.text}::{toks[k + 2].text}" + ("()" if is_call else "")
            if name not in out and toks[k + 2].text.lower() != "class":
                out.append(name)
            k += 3
            continue
        if (tk.kind == "ident" and tk.text.isupper() and tk.text.lower() not in ("true", "false", "null")
                and not (k + 1 < len(toks) and toks[k + 1].text == "(") and tk.text not in out):
            out.append(tk.text)
        k += 1
    return out


# --------------------------------------------------------------------------- statements


@dataclass
class Frame:
    kind: str  # class | function | if | switch | loop | block
    line: int
    braced: bool = True
    alt: bool = False  # alternative syntax (if: ... endif;)
    name: str | None = None
    conds: list[str] = field(default_factory=list)  # effective conditions this frame contributes
    cond_toks: list[PTok] = field(default_factory=list)
    chain: list[str] | None = None  # if/elseif conditions so far, for a following else
    text: str | None = None  # loop / catch description
    # switch bookkeeping
    subject: str | None = None
    subject_toks: list[PTok] = field(default_factory=list)
    case_conds: list[str] = field(default_factory=list)
    prev_case_conds: list[str] = field(default_factory=list)
    case_open: bool = False
    case_line: int = 0
    guards: list[str] = field(default_factory=list)   # NOT(cond) of earlier conditional returns in a function


@dataclass
class PhpFinding:
    kind: str  # calculation | assignment | constant
    name: str
    expression: str
    rhs_toks: list[PTok]
    conditions: list[str]
    cond_toks: list[PTok]
    condition_lines: list[int]
    loops: list[str]
    start_line: int
    end_line: int
    cls: str | None
    function: str | None
    anchor: str
    confidence: float
    reasons: list[str]
    target_key: str | None = None  # "$this->x" / "$x" for modification tracking
    is_return: bool = False


class PhpParser:
    def __init__(self, toks: list[PTok]):
        self.t = toks
        self.n = len(toks)
        self.frames: list[Frame] = []
        self.pending_chain: list[str] | None = None
        self.findings: list[PhpFinding] = []
        self.constants: dict[str, tuple[str, int]] = {}  # "Class::NAME" or "NAME" -> (value, line)
        self.property_defaults: dict[str, tuple[str, int]] = {}  # "Class::$prop" -> (value, line)
        self.modified: set[str] = set()  # "Class::$prop" assigned via $this->prop
        self.classes: list[str] = []
        self.functions: list[str] = []
        self.data_moves = 0

    # -- helpers
    def _tok(self, i: int) -> PTok | None:
        return self.t[i] if 0 <= i < self.n else None

    def _cls(self) -> str | None:
        return next((f.name for f in reversed(self.frames) if f.kind == "class"), None)

    def _fn(self) -> str | None:
        return next((f.name for f in reversed(self.frames) if f.kind == "function"), None)

    def _paren(self, i: int) -> tuple[list[PTok], int]:
        tk = self._tok(i)
        if tk is None or tk.text != "(":
            return [], i
        depth, j = 0, i
        while j < self.n:
            if self.t[j].text == "(":
                depth += 1
            elif self.t[j].text == ")":
                depth -= 1
                if depth == 0:
                    return self.t[i + 1 : j], j + 1
            j += 1
        return self.t[i + 1 :], self.n

    def _statement(self, i: int) -> tuple[list[PTok], int]:
        """Tokens up to ';' at depth 0. Stops before an unmatched '}' (missing semicolon)."""
        depth, out = 0, []
        while i < self.n:
            tk = self.t[i]
            if tk.text in ("(", "[", "{"):
                depth += 1
            elif tk.text in (")", "]", "}"):
                if depth == 0:
                    return out, i
                depth -= 1
            elif tk.text == ";" and depth == 0:
                return out, i + 1
            out.append(tk)
            i += 1
        return out, i

    def _context(self) -> tuple[list[str], list[PTok], list[int], list[str]]:
        conds: list[str] = []
        toks: list[PTok] = []
        lines: list[int] = []
        loops: list[str] = []
        for f in self.frames:
            if f.kind == "function":
                conds.extend(f.guards)
            elif f.kind == "if":
                conds.extend(f.conds)
                toks.extend(f.cond_toks)
                lines.append(f.line)
            elif f.kind == "switch" and f.case_conds:
                conds.append(f.case_conds[0] if len(f.case_conds) == 1 else "(" + ") OR (".join(f.case_conds) + ")")
                toks.extend(f.subject_toks)
                lines.append(f.case_line)
            elif f.kind == "loop":
                loops.append(f"{f.text} (line {f.line})")
        return conds, toks, lines, loops

    def _open_body(self, i: int, frame: Frame) -> int:
        tk = self._tok(i)
        if tk is not None and tk.text == "{":
            frame.braced = True
            i += 1
        elif tk is not None and tk.text == ":":
            frame.braced, frame.alt = True, True
            i += 1
        else:
            frame.braced = False
        self.frames.append(frame)
        return i

    def _statement_done(self, next_i: int) -> None:
        """Pop brace-less (single statement) frames whose statement just finished."""
        nxt = self._tok(next_i)
        else_follows = nxt is not None and nxt.kind == "ident" and nxt.text.lower() in ("else", "elseif")
        while self.frames and not self.frames[-1].braced:
            f = self.frames.pop()
            if f.kind == "if":
                self.pending_chain = f.chain
                if else_follows:
                    break

    def _close(self) -> None:
        while self.frames and not self.frames[-1].braced:
            self.frames.pop()
        if not self.frames:
            return
        f = self.frames.pop()
        self.pending_chain = f.chain if f.kind == "if" else None

    # -- main loop
    def run(self) -> "PhpParser":
        i = 0
        while i < self.n:
            tk = self.t[i]
            low = tk.text.lower() if tk.kind == "ident" else tk.text
            if low not in ("else", "elseif", "}"):
                self.pending_chain = None
            top = self.frames[-1] if self.frames else None
            if top is not None and top.kind == "switch" and low not in ("case", "default", "}"):
                top.case_open = False
            if tk.kind == "attr":
                depth = 1
                i += 1
                while i < self.n and depth:
                    depth += {"[": 1, "]": -1}.get(self.t[i].text, 0)
                    i += 1
                continue
            if tk.text == "}":
                i += 1
                self._close()
                self._statement_done(i)
                continue
            if tk.text == "{":
                self.frames.append(Frame("block", tk.line))
                i += 1
                continue
            if tk.text == ";":
                i += 1
                self._statement_done(i)
                continue
            if tk.kind == "ident":
                handler = self._handlers.get(low)
                if handler is not None and not (self._tok(i + 1) is not None and self.t[i + 1].text == "::"):
                    i = handler(self, i)
                    continue
            i = self._expression_statement(i)
        return self

    # -- handlers
    def _skip_modifier(self, i: int) -> int:
        return i + 1

    def _skip_statement(self, i: int) -> int:
        _, j = self._statement(i + 1)
        self._statement_done(j)
        return j

    def _namespace(self, i: int) -> int:
        j = i + 1
        while j < self.n and self.t[j].text not in (";", "{"):
            j += 1
        if j < self.n and self.t[j].text == "{":
            self.frames.append(Frame("block", self.t[i].line))
        return j + 1

    def _class(self, i: int) -> int:
        name_tok = self._tok(i + 1)
        name = name_tok.text if name_tok is not None and name_tok.kind == "ident" else "{anonymous}"
        j = i + 1
        while j < self.n and self.t[j].text != "{":
            j += 1
        self.frames.append(Frame("class", self.t[i].line, name=name))
        self.classes.append(name)
        return j + 1

    def _function(self, i: int) -> int:
        line = self.t[i].line
        j = i + 1
        if self._tok(j) is not None and self.t[j].text == "&":
            j += 1
        name = "{closure}"
        if self._tok(j) is not None and self.t[j].kind == "ident":
            name = self.t[j].text
            j += 1
        _, j = self._paren(j)
        while j < self.n and self.t[j].text not in ("{", ";"):
            if self.t[j].text == "(":
                _, j = self._paren(j)
                continue
            j += 1
        if j < self.n and self.t[j].text == ";":
            return j + 1  # abstract / interface method
        self.frames.append(Frame("function", line, name=name))
        self.functions.append(f"{self._cls()}::{name}" if self._cls() else name)
        return j + 1

    def _if(self, i: int) -> int:
        line = self.t[i].line
        cond, j = self._paren(i + 1)
        text = render(cond)
        frame = Frame("if", line, conds=[text], cond_toks=list(cond), chain=[text])
        return self._open_body(j, frame)

    def _else(self, i: int) -> int:
        line = self.t[i].line
        top = self.frames[-1] if self.frames else None
        if top is not None and top.kind == "if" and top.alt:  # else: / elseif(): in alternative syntax
            self.frames.pop()
            self.pending_chain = top.chain
        chain = self.pending_chain or []
        self.pending_chain = None
        is_elseif = self.t[i].text.lower() == "elseif"
        j = i + 1
        if not is_elseif and self._tok(j) is not None and self.t[j].kind == "ident" and self.t[j].text.lower() == "if":
            is_elseif, j = True, j + 1
        negated = [f"NOT ({c})" for c in chain]
        if is_elseif:
            cond, j = self._paren(j)
            text = render(cond)
            frame = Frame("if", line, conds=negated + [text], cond_toks=list(cond), chain=chain + [text])
        else:
            frame = Frame("if", line, conds=negated or ["else"], chain=None)
        return self._open_body(j, frame)

    def _alt_end(self, i: int) -> int:
        self._close()
        j = i + 1
        if self._tok(j) is not None and self.t[j].text == ";":
            j += 1
        self._statement_done(j)
        return j

    def _switch(self, i: int) -> int:
        subj, j = self._paren(i + 1)
        frame = Frame("switch", self.t[i].line, subject=render(subj), subject_toks=list(subj))
        return self._open_body(j, frame)

    def _switch_frame(self) -> Frame | None:
        return next((f for f in reversed(self.frames) if f.kind == "switch"), None)

    def _case(self, i: int) -> int:
        sw = self._switch_frame()
        line = self.t[i].line
        j, depth, val = i + 1, 0, []
        while j < self.n:
            tk = self.t[j]
            if tk.text in ("(", "["):
                depth += 1
            elif tk.text in (")", "]"):
                depth -= 1
            elif depth == 0 and tk.text in (":", ";"):
                j += 1
                break
            val.append(tk)
            j += 1
        if sw is None:
            return j
        subj = sw.subject or ""
        cond = render(val) if subj.lower() == "true" else f"{subj} == {render(val)}"
        if sw.case_open:
            sw.case_conds.append(cond)
        else:
            sw.prev_case_conds.extend(sw.case_conds)
            sw.case_conds = [cond]
        sw.case_open, sw.case_line = True, line
        return j

    def _default(self, i: int) -> int:
        sw = self._switch_frame()
        j = i + 1
        if self._tok(j) is not None and self.t[j].text in (":", ";"):
            j += 1
        if sw is not None:
            sw.prev_case_conds.extend(sw.case_conds)
            prev = sw.prev_case_conds
            sw.case_conds = [f"NOT ({' OR '.join(prev)})" if prev else "default"]
            sw.case_open, sw.case_line = True, self.t[i].line
        return j

    def _loop(self, i: int) -> int:
        kw = self.t[i].text.lower()
        hdr, j = self._paren(i + 1)
        if kw == "while" and self._tok(j) is not None and self.t[j].text == ";":  # tail of do { } while (...);
            self._statement_done(j + 1)
            return j + 1
        return self._open_body(j, Frame("loop", self.t[i].line, text=f"{kw} ({render(hdr)})"))

    def _do(self, i: int) -> int:
        return self._open_body(i + 1, Frame("loop", self.t[i].line, text="do-while"))

    def _try(self, i: int) -> int:
        kw = self.t[i].text.lower()
        j = i + 1
        text = kw
        if kw == "catch":
            hdr, j = self._paren(j)
            text = f"catch ({render(hdr)})"
        return self._open_body(j, Frame("block", self.t[i].line, text=text))

    def _const(self, i: int) -> int:
        toks, j = self._statement(i + 1)
        cls = self._cls()
        depth, part = 0, []
        parts: list[list[PTok]] = []
        for tk in toks:
            if tk.text in ("(", "["):
                depth += 1
            elif tk.text in (")", "]"):
                depth -= 1
            if tk.text == "," and depth == 0:
                parts.append(part)
                part = []
            else:
                part.append(tk)
        parts.append(part)
        for p in parts:
            eq = next((k for k, tk in enumerate(p) if tk.text == "="), None)
            if eq is None or eq == 0:
                continue
            name_tok, value = p[eq - 1], p[eq + 1 :]
            full = f"{cls}::{name_tok.text}" if cls else name_tok.text
            self.constants[full] = (render(value), name_tok.line)
            self._add(kind="constant", name=full, expression=render(value), rhs=value, start=self.t[i].line,
                      end=max(tk.line for tk in p), anchor=name_tok.text, confidence=0.95,
                      reasons=["class/global constant declaration"], context_free=True)
        self._statement_done(j)
        return j

    def _return(self, i: int) -> int:
        toks, j = self._statement(i + 1)
        fn = self._fn()
        if fn is not None and toks:
            conds = self._context()[0]
            if has_arithmetic(toks):
                kind, conf, why = "calculation", 0.85, ["deterministic parse of arithmetic return"]
            elif has_conditional(toks):
                kind, conf, why = "calculation", 0.75, ["conditional expression (ternary / match / ??)"]
            elif (is_literal(toks) or is_constant_ref(toks)) and (conds or render(toks).lower() not in TRIVIAL_LITERALS):
                kind, conf, why = "assignment", 0.8, ["literal return value"]
            elif conds:
                kind, conf, why = "assignment", 0.6, ["conditional return of a variable"]
            else:
                kind = None
            if kind:
                cls = self._cls()
                self._add(kind=kind, name=f"{cls}::{fn}()" if cls else f"{fn}()", expression=render(toks), rhs=toks,
                          start=self.t[i].line, end=max(tk.line for tk in toks), anchor="return",
                          confidence=conf, reasons=why, is_return=True)
            fn_idx = next((k for k in range(len(self.frames) - 1, -1, -1) if self.frames[k].kind == "function"), None)
            if fn_idx is not None:
                inner = [c for f in self.frames[fn_idx + 1:] if f.kind == "if" for c in f.conds]
                if inner:   # an early return guards every later statement in the function
                    self.frames[fn_idx].guards.append(f"NOT ({' AND '.join(inner)})")
        self._statement_done(j)
        return j

    def _expression_statement(self, i: int) -> int:
        toks, j = self._statement(i)
        if j == i:  # unmatched closer; avoid an infinite loop
            return i + 1
        top = self.frames[-1] if self.frames else None
        if top is not None and top.kind == "class":
            self._property(toks)
        elif toks and toks[0].kind == "ident" and toks[0].text.lower() == "define":
            self._define(toks)
        elif toks:
            self._assignment(toks)
        self._statement_done(j)
        return j

    # -- analysis of statements
    def _property(self, toks: list[PTok]) -> None:
        cls = self._cls()
        for k, tk in enumerate(toks):
            if tk.kind == "var" and k + 1 < len(toks) and toks[k + 1].text == "=":
                value, m, depth = [], k + 2, 0
                while m < len(toks) and not (toks[m].text == "," and depth == 0):
                    depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(toks[m].text, 0)
                    value.append(toks[m])
                    m += 1
                if value and (is_literal(value) or is_constant_ref(value)):
                    key = f"{cls}::{tk.text}"
                    self.property_defaults[key] = (render(value), tk.line)
                    self._add(kind="constant", name=key, expression=render(value), rhs=value, start=tk.line,
                              end=max(x.line for x in value), anchor=tk.text, confidence=0.7,
                              reasons=["property default value"], context_free=True, target_key=key)

    def _define(self, toks: list[PTok]) -> None:
        args, _ = self._paren_in(toks, 1)
        if len(args) >= 3 and args[0].kind == "str" and args[1].text == ",":
            name = args[0].text.strip("'\"")
            value = args[2:]
            self.constants[name] = (render(value), toks[0].line)
            self._add(kind="constant", name=name, expression=render(value), rhs=value, start=toks[0].line,
                      end=max(x.line for x in toks), anchor=name, confidence=0.95,
                      reasons=["define() constant"], context_free=True)

    @staticmethod
    def _paren_in(toks: list[PTok], k: int) -> tuple[list[PTok], int]:
        if k >= len(toks) or toks[k].text != "(":
            return [], k
        depth = 0
        for m in range(k, len(toks)):
            depth += {"(": 1, ")": -1}.get(toks[m].text, 0)
            if depth == 0:
                return toks[k + 1 : m], m + 1
        return toks[k + 1 :], len(toks)

    def _assignment(self, toks: list[PTok]) -> None:
        depth, op_idx = 0, None
        for k, tk in enumerate(toks):
            if tk.text in ("(", "[", "{"):
                depth += 1
            elif tk.text in (")", "]", "}"):
                depth -= 1
            elif depth == 0 and tk.kind == "op" and tk.text in ASSIGN_OPS:
                op_idx = k
                break
        if not op_idx or toks[0].kind != "var":
            return
        lhs, op, rhs = toks[:op_idx], toks[op_idx].text, toks[op_idx + 1 :]
        if not rhs or (rhs[0].kind == "ident" and rhs[0].text.lower() in ("new", "function", "fn", "clone")):
            return
        target = render(lhs)
        cls = self._cls()
        key = None
        if len(lhs) >= 3 and lhs[0].text == "$this" and lhs[1].text in ("->", "?->") and lhs[2].kind == "ident":
            key = f"{cls}::${lhs[2].text}"
            self.modified.add(key)
        rhs_text = render(rhs)
        if op in ARITH_ASSIGN:
            compound = has_arithmetic(rhs) or has_conditional(rhs)
            expr = f"{target} {ARITH_ASSIGN[op]} ({rhs_text})" if compound else f"{target} {ARITH_ASSIGN[op]} {rhs_text}"
        elif op == "??=":
            expr = f"{target} ?? {rhs_text}"
        elif op == ".=":
            expr = f"{target} . {rhs_text}"
        else:
            expr = rhs_text
        conds = self._context()[0]
        if op in ARITH_ASSIGN or has_arithmetic(rhs):
            kind, conf, why = "calculation", 0.85, ["deterministic parse of arithmetic assignment"]
        elif has_conditional(rhs):
            kind, conf, why = "calculation", 0.75, ["conditional expression (ternary / match / ??)"]
        elif is_literal(rhs) or is_constant_ref(rhs):
            if not conds and rhs_text.lower() in TRIVIAL_LITERALS:
                self.data_moves += 1
                return
            kind, conf, why = "assignment", 0.8, ["literal assignment"]
        elif conds:
            kind, conf, why = "assignment", 0.6, ["conditional data move (no literal)"]
        else:
            self.data_moves += 1
            return
        anchor = next((tk.text for tk in reversed(lhs) if tk.kind in ("ident", "var")), lhs[0].text)
        self._add(kind=kind, name=target, expression=expr, rhs=rhs, start=toks[0].line,
                  end=max(tk.line for tk in toks), anchor=anchor, confidence=conf, reasons=why, target_key=key)

    def _add(self, *, kind: str, name: str, expression: str, rhs: list[PTok], start: int, end: int, anchor: str,
             confidence: float, reasons: list[str], context_free: bool = False, target_key: str | None = None,
             is_return: bool = False) -> None:
        conds, ctoks, clines, loops = ([], [], [], []) if context_free else self._context()
        self.findings.append(PhpFinding(
            kind=kind, name=name, expression=expression, rhs_toks=rhs, conditions=conds, cond_toks=ctoks,
            condition_lines=clines, loops=loops, start_line=start, end_line=end, cls=self._cls(),
            function=self._fn(), anchor=anchor, confidence=confidence, reasons=reasons,
            target_key=target_key, is_return=is_return,
        ))

    _handlers = {
        "namespace": _namespace,
        "class": _class, "interface": _class, "trait": _class, "enum": _class,
        "function": _function,
        "abstract": _skip_modifier, "final": _skip_modifier, "public": _skip_modifier, "private": _skip_modifier,
        "protected": _skip_modifier, "readonly": _skip_modifier, "var": _skip_modifier, "static": _skip_modifier,
        "if": _if, "else": _else, "elseif": _else,
        "endif": _alt_end, "endforeach": _alt_end, "endwhile": _alt_end, "endfor": _alt_end, "endswitch": _alt_end,
        "switch": _switch, "case": _case, "default": _default,
        "for": _loop, "foreach": _loop, "while": _loop, "do": _do,
        "try": _try, "catch": _try, "finally": _try,
        "const": _const, "return": _return,
        "use": _skip_statement, "echo": _skip_statement, "print": _skip_statement, "throw": _skip_statement,
        "break": _skip_statement, "continue": _skip_statement, "unset": _skip_statement, "global": _skip_statement,
        "require": _skip_statement, "require_once": _skip_statement, "include": _skip_statement,
        "include_once": _skip_statement, "declare": _skip_statement, "goto": _skip_statement,
    }
