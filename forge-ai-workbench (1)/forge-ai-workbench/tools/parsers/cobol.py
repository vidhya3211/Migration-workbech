"""Deterministic COBOL reader.

Three layers:
  1. ``normalise``        -- fixed/free format, comment and sequence-area stripping, line numbers kept
  2. ``parse_data_items`` -- DATA DIVISION entries (levels, PIC, VALUE, 88-level condition names)
  3. ``ProcedureParser``  -- PROCEDURE DIVISION statements with the IF/EVALUATE condition stack that
                             guards each one, so every computation is reported with *when* it applies
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- source normalisation


@dataclass
class SrcLine:
    no: int
    raw: str
    code: str


def detect_fixed_format(lines: list[str]) -> bool:
    sample = [ln for ln in lines if ln.strip()][:400]
    if not sample:
        return True
    ok = 0
    for ln in sample:
        head, ind = ln[:6], ln[6:7]
        head_ok = re.fullmatch(r"[\d ]*", head) is not None or (len(head) == 6 and head.isalnum())
        if head_ok and ind in ("", " ", "*", "-", "/", "D", "d"):
            ok += 1
    return ok / len(sample) >= 0.9


def _strip_inline_comment(code: str) -> str:
    quote = None
    for i, ch in enumerate(code):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif code.startswith("*>", i):
            return code[:i]
    return code


def normalise(text: str) -> tuple[list[SrcLine], bool]:
    raw_lines = text.split("\n")
    fixed = detect_fixed_format(raw_lines)
    out: list[SrcLine] = []
    for no, raw in enumerate(raw_lines, 1):
        if fixed:
            ind = raw[6:7]
            code = raw[7:72]
            if ind in ("*", "/", "D", "d"):
                code = ""
            elif ind == "-":
                code = " " + code.lstrip()
        else:
            code = "" if raw.lstrip().startswith("*>") else raw
        out.append(SrcLine(no, raw, _strip_inline_comment(code)))
    return out, fixed


# --------------------------------------------------------------------------- tokens


@dataclass
class Tok:
    kind: str  # word | num | str | op | dot
    text: str
    line: int

    @property
    def upper(self) -> str:
        return self.text.upper()


_TOKEN = re.compile(
    r"""
      (?P<str>'(?:[^']|'')*'|"(?:[^"]|"")*")
    | (?P<num>(?:\d+(?:\.\d+)?|\.\d+)(?![A-Za-z0-9_-]))
    | (?P<word>[A-Za-z0-9_](?:[A-Za-z0-9_-]*[A-Za-z0-9_])?)
    | (?P<op>\*\*|>=|<=|<>|[-+*/=<>():,&])
    | (?P<dot>\.(?=\s|$))
    | (?P<qual>\.)
    | (?P<ws>\s+)
    | (?P<other>.)
    """,
    re.VERBOSE,
)


def tokenize(lines: list[SrcLine]) -> list[Tok]:
    toks: list[Tok] = []
    for sl in lines:
        for m in _TOKEN.finditer(sl.code):
            kind = m.lastgroup
            if kind in ("ws", "other"):
                continue
            toks.append(Tok("op" if kind == "qual" else kind, m.group(), sl.no))
    return toks


def render(toks: list[Tok]) -> str:
    out = ""
    prev: Tok | None = None
    prev2: Tok | None = None
    for tk in toks:
        s = tk.text
        unary = (
            prev is not None and prev.text in ("-", "+") and tk.kind == "num"
            and (prev2 is None or (prev2.kind == "op" and prev2.text != ")") or prev2.upper in ("BY", "TO", "FROM", "VALUE"))
        )
        if prev is None:
            out = s
        elif s in (")", ",") or prev.text == "(" or unary:
            out += s
        else:
            out += " " + s
        prev2, prev = prev, tk
    return out


FIGURATIVE = {
    "ZERO", "ZEROS", "ZEROES", "SPACE", "SPACES", "HIGH-VALUE", "HIGH-VALUES", "LOW-VALUE", "LOW-VALUES",
    "QUOTE", "QUOTES", "NULL", "NULLS",
}
INIT_FIGURATIVE = FIGURATIVE - {"QUOTE", "QUOTES"}

KEYWORDS = FIGURATIVE | {
    "AND", "OR", "NOT", "IS", "ARE", "EQUAL", "EQUALS", "TO", "GREATER", "LESS", "THAN", "NUMERIC", "ALPHABETIC",
    "ALPHABETIC-LOWER", "ALPHABETIC-UPPER", "POSITIVE", "NEGATIVE", "THRU", "THROUGH", "TRUE", "FALSE", "OTHER",
    "ALSO", "ANY", "FUNCTION", "OF", "IN", "ROUNDED", "ALL", "BY", "FROM", "INTO", "GIVING", "REMAINDER", "UNTIL",
    "VARYING", "TIMES", "AFTER", "BEFORE", "WITH", "TEST", "OMITTED", "LENGTH", "ADDRESS", "THEN", "WHEN",
}


def is_literal(toks: list[Tok]) -> bool:
    if len(toks) == 1:
        return toks[0].kind in ("num", "str") or toks[0].upper in FIGURATIVE
    if len(toks) == 2:
        return (toks[0].text in ("-", "+") and toks[1].kind == "num") or (toks[0].upper == "ALL" and toks[1].kind == "str")
    return False


def operands(toks: list[Tok]) -> list[list[Tok]]:
    """Split ``A B(I) C OF D 'X'`` into operands, keeping subscripts and qualification."""
    out: list[list[Tok]] = []
    i = 0
    while i < len(toks):
        tk = toks[i]
        if tk.text == ",":
            i += 1
            continue
        cur = [tk]
        i += 1
        if tk.upper == "FUNCTION" and i < len(toks):
            cur.append(toks[i])
            i += 1
        if tk.text in ("-", "+") and i < len(toks) and toks[i].kind == "num":
            cur.append(toks[i])
            i += 1
        while i < len(toks):
            if toks[i].text == "(":
                depth = 0
                while i < len(toks):
                    cur.append(toks[i])
                    if toks[i].text == "(":
                        depth += 1
                    elif toks[i].text == ")":
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                    i += 1
            elif toks[i].upper in ("OF", "IN") and i + 1 < len(toks):
                cur.extend(toks[i : i + 2])
                i += 2
            else:
                break
        out.append(cur)
    return out


# --------------------------------------------------------------------------- data division


@dataclass
class DataItem:
    level: int
    name: str
    pic: str | None
    usage: str | None
    value: str | None
    values: list[str]
    occurs: str | None
    redefines: str | None
    start_line: int
    end_line: int
    section: str | None
    parent: str | None
    source: str  # program or copybook member that defines it


def split_sentences(lines: list[SrcLine]) -> list[tuple[str, int, int]]:
    """Split code into period-terminated sentences, tracking line range."""
    out: list[tuple[str, int, int]] = []
    buf: list[str] = []
    start = end = 0
    for sl in lines:
        code = sl.code
        if not code.strip():
            continue
        seg_start = 0
        quote = None
        for i, ch in enumerate(code):
            if quote:
                if ch == quote:
                    quote = None
                continue
            if ch in ("'", '"'):
                quote = ch
            elif ch == "." and (i + 1 == len(code) or code[i + 1].isspace()):
                seg = code[seg_start:i]
                if seg.strip():
                    if not buf:
                        start = sl.no
                    buf.append(seg.strip())
                    end = sl.no
                if buf:
                    out.append((" ".join(buf), start, end))
                    buf = []
                seg_start = i + 1
        rest = code[seg_start:]
        if rest.strip():
            if not buf:
                start = sl.no
            buf.append(rest.strip())
            end = sl.no
    if buf:
        out.append((" ".join(buf), start, end))
    return out


_LITERAL_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_CLAUSE_WORDS = {
    "PIC", "PICTURE", "VALUE", "VALUES", "USAGE", "COMP", "COMP-1", "COMP-2", "COMP-3", "COMP-4", "COMP-5",
    "COMPUTATIONAL", "BINARY", "PACKED-DECIMAL", "OCCURS", "REDEFINES", "INDEXED", "SIGN", "JUSTIFIED", "BLANK",
}


def _value_list(rest: str, pos: int) -> list[str]:
    toks = [Tok(m.lastgroup, m.group(), 0) for m in _TOKEN.finditer(rest[pos:]) if m.lastgroup not in ("ws", "other")]
    values: list[str] = []
    i = 0
    while i < len(toks):
        tk = toks[i]
        if tk.text == ",":
            i += 1
            continue
        if tk.text in ("-", "+") and i + 1 < len(toks) and toks[i + 1].kind == "num":
            val, i = tk.text + toks[i + 1].text, i + 2
        elif tk.kind in ("num", "str") or tk.upper in FIGURATIVE:
            val, i = tk.text, i + 1
        elif tk.upper == "ALL" and i + 1 < len(toks):
            val, i = f"ALL {toks[i + 1].text}", i + 2
        else:
            break
        if i < len(toks) and toks[i].upper in ("THRU", "THROUGH") and i + 1 < len(toks):
            val, i = f"{val} THRU {toks[i + 1].text}", i + 2
        values.append(val)
    return values


def parse_data_items(sentences: list[tuple[str, int, int]], source: str) -> tuple[list[DataItem], list[str]]:
    """Return (items, copy_members)."""
    items: list[DataItem] = []
    copies: list[str] = []
    section: str | None = None
    stack: list[DataItem] = []
    last_elementary: DataItem | None = None
    for text, start, end in sentences:
        t = text.strip()
        u = t.upper()
        msec = re.fullmatch(r"([A-Z0-9-]+)\s+SECTION", u)
        if msec:
            section = msec.group(1)
            stack.clear()
            continue
        mcopy = re.match(r"COPY\s+['\"]?([A-Z0-9_-]+)", u)
        if mcopy:
            copies.append(mcopy.group(1))
            continue
        m = re.match(r"(\d{1,2})(?:\s+(.*))?$", t, re.S)
        if not m:
            continue
        level = int(m.group(1))
        if not (1 <= level <= 49 or level in (66, 77, 88)):
            continue
        rest = m.group(2) or ""
        first = rest.split(None, 1)[0] if rest.strip() else ""
        if first and first.upper() not in _CLAUSE_WORDS and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", first):
            name, rest = first, rest[len(first):]
        else:
            name = "FILLER"
        blank = _LITERAL_RE.sub(lambda mm: " " * len(mm.group()), rest).upper()
        pic = re.search(r"\bPIC(?:TURE)?\s+(?:IS\s+)?(\S+)", blank)
        usage = re.search(r"\b(COMP(?:UTATIONAL)?(?:-[1-5X])?|BINARY|PACKED-DECIMAL|INDEX|POINTER)\b", blank)
        occurs = re.search(r"\bOCCURS\s+(\d+(?:\s+TO\s+\d+)?)", blank)
        redefines = re.search(r"\bREDEFINES\s+([A-Z0-9-]+)", blank)
        vpos = re.search(r"\bVALUES?\b(?:\s+(?:IS|ARE)\b)?", blank)
        values = _value_list(rest, vpos.end()) if vpos else []

        if level == 88:
            parent = last_elementary.name if last_elementary else None
        else:
            if level in (1, 77, 66):
                stack.clear()
            while stack and stack[-1].level >= level:
                stack.pop()
            parent = stack[-1].name if stack else None

        item = DataItem(
            level=level, name=name, pic=rest[pic.start(1):pic.end(1)] if pic else None,
            usage=usage.group(1) if usage else None, value=values[0] if values and level != 88 else None,
            values=values, occurs=occurs.group(1) if occurs else None,
            redefines=redefines.group(1) if redefines else None, start_line=start, end_line=end,
            section=section, parent=parent, source=source,
        )
        items.append(item)
        if level != 88:
            stack.append(item)
            last_elementary = item
    return items, copies


# --------------------------------------------------------------------------- procedure division

VERBS = frozenset(
    """ACCEPT ADD ALTER CALL CANCEL CLOSE COMPUTE CONTINUE COPY DELETE DISPLAY DIVIDE ELSE EVALUATE EXEC EXIT
    GENERATE GO GOBACK IF INITIALIZE INITIATE INSPECT INVOKE MERGE MOVE MULTIPLY NEXT OPEN PERFORM READ RELEASE
    RETURN REWRITE SEARCH SET SORT START STOP STRING SUBTRACT TERMINATE UNSTRING WHEN WRITE""".split()
)
ARITH_STOPS = frozenset({"ON", "SIZE", "NOT", "AT", "INVALID"})
ROUNDING_NOISE = frozenset({
    "ROUNDED", "MODE", "IS", "AWAY-FROM-ZERO", "NEAREST-AWAY-FROM-ZERO", "NEAREST-EVEN", "NEAREST-TOWARD-ZERO",
    "PROHIBITED", "TOWARD-GREATER", "TOWARD-LESSER", "TRUNCATION",
})
PHRASES = sorted(
    [
        ("NOT", "ON", "SIZE", "ERROR"), ("NOT", "SIZE", "ERROR"), ("ON", "SIZE", "ERROR"), ("SIZE", "ERROR"),
        ("NOT", "AT", "END"), ("AT", "END"), ("NOT", "INVALID", "KEY"), ("INVALID", "KEY"),
        ("NOT", "ON", "EXCEPTION"), ("ON", "EXCEPTION"), ("NOT", "ON", "OVERFLOW"), ("ON", "OVERFLOW"),
    ],
    key=len, reverse=True,
)


@dataclass
class Frame:
    kind: str  # IF | EVAL | LOOP | PHRASE
    text: str
    toks: list[Tok]
    line: int
    branch: str = "then"
    # EVALUATE bookkeeping
    cur_conds: list[str] = field(default_factory=list)
    prev_conds: list[str] = field(default_factory=list)
    when_toks: list[Tok] = field(default_factory=list)
    when_line: int = 0
    last_when_end: int = -1
    is_other: bool = False


@dataclass
class ProcFinding:
    kind: str  # calculation | assignment
    verb: str
    target: str
    expression: str
    expr_toks: list[Tok]
    conditions: list[str]
    cond_toks: list[Tok]
    condition_lines: list[int]
    loops: list[str]
    start_line: int
    end_line: int
    paragraph: str | None
    anchor: str
    notes: list[str] = field(default_factory=list)


@dataclass
class Call:
    caller: str | None
    target: str
    thru: str | None
    line: int
    clause: str
    conditions: list[str]


def _when_to_cond(subject: str, when: str) -> str:
    su, wu = subject.upper(), when.upper()
    if su == "TRUE":
        return when
    if su == "FALSE":
        return f"NOT ({when})"
    if wu == "ANY":
        return f"{subject} = ANY"
    m = re.match(r"^(.+?)\s+(?:THRU|THROUGH)\s+(.+)$", when, re.I)
    if m:
        return f"{m.group(1)} <= {subject} <= {m.group(2)}"
    if wu.startswith("NOT "):
        return f"{subject} NOT = {when[4:]}"
    return f"{subject} = {when}"


class ProcedureParser:
    def __init__(self, toks: list[Tok], dd: dict[str, DataItem]):
        self.t = toks
        self.n = len(toks)
        self.dd = dd
        self.frames: list[Frame] = []
        self.para: str | None = None
        self.findings: list[ProcFinding] = []
        self.paragraphs: dict[str, int] = {}
        self.calls: list[Call] = []
        self.copies: list[str] = []
        self.sql_blocks = 0
        self.sql_tables: set[str] = set()
        self.modified: set[str] = set()
        self.data_moves = 0
        self.skipped: list[tuple[int, str]] = []

    # -- helpers
    def _tok(self, i: int) -> Tok | None:
        return self.t[i] if 0 <= i < self.n else None

    @staticmethod
    def _boundary(tk: Tok, extra: frozenset[str]) -> bool:
        if tk.kind == "dot":
            return True
        if tk.kind != "word":
            return False
        u = tk.upper
        return u in VERBS or u.startswith("END-") or u in extra

    def _collect(self, i: int, extra: frozenset[str] = frozenset()) -> tuple[list[Tok], int]:
        out: list[Tok] = []
        depth = 0
        while i < self.n:
            tk = self.t[i]
            if depth == 0 and self._boundary(tk, extra):
                break
            if tk.text == "(":
                depth += 1
            elif tk.text == ")":
                depth = max(0, depth - 1)
            out.append(tk)
            i += 1
        return out, i

    def _phrase_len(self, i: int) -> int:
        for ph in PHRASES:
            if all((tk := self._tok(i + k)) is not None and tk.upper == w for k, w in enumerate(ph)):
                return len(ph)
        return 0

    def _context(self) -> tuple[list[str], list[Tok], list[int], list[str]]:
        conds: list[str] = []
        toks: list[Tok] = []
        lines: list[int] = []
        loops: list[str] = []
        for f in self.frames:
            if f.kind == "IF":
                conds.append(f.text if f.branch == "then" else f"NOT ({f.text})")
                toks.extend(f.toks)
                lines.append(f.line)
            elif f.kind == "EVAL" and (f.cur_conds or f.is_other):
                if f.is_other:
                    conds.append(f"NOT ({' OR '.join(f.prev_conds)})" if f.prev_conds else "WHEN OTHER")
                else:
                    if f.prev_conds:   # EVALUATE is first-match: an earlier WHEN that matched wins
                        conds.append(f"NOT ({' OR '.join(f.prev_conds)})")
                    conds.append(f.cur_conds[0] if len(f.cur_conds) == 1 else "(" + ") OR (".join(f.cur_conds) + ")")
                toks.extend(f.toks + f.when_toks)
                lines.append(f.when_line)
            elif f.kind == "PHRASE":
                conds.append(f.text)
                lines.append(f.line)
            elif f.kind == "LOOP":
                loops.append(f"{f.text} (line {f.line})")
        return conds, toks, lines, loops

    def _mark_modified(self, target_toks: list[Tok]) -> None:
        if target_toks:
            self.modified.add(target_toks[0].upper)

    def _emit(self, kind: str, verb: str, target_toks: list[Tok], expr: str, expr_toks: list[Tok],
              start_line: int, end_line: int, notes: list[str] | None = None) -> None:
        conds, ctoks, clines, loops = self._context()
        self._mark_modified(target_toks)
        self.findings.append(ProcFinding(
            kind=kind, verb=verb, target=render(target_toks), expression=expr, expr_toks=expr_toks,
            conditions=conds, cond_toks=ctoks, condition_lines=clines, loops=loops, start_line=start_line,
            end_line=end_line, paragraph=self.para, anchor=target_toks[0].text, notes=notes or [],
        ))

    # -- main loop
    def run(self) -> "ProcedureParser":
        i = 0
        at_start = True
        while i < self.n:
            tk = self.t[i]
            u = tk.upper
            if tk.kind == "dot":
                self.frames.clear()
                at_start = True
                i += 1
                continue
            if at_start and tk.kind == "word" and u not in VERBS:
                nxt = self._tok(i + 1)
                if nxt is not None and nxt.kind == "dot":
                    self._label(tk)
                    i += 2
                    continue
                nxt2 = self._tok(i + 2)
                if nxt is not None and nxt.upper == "SECTION" and nxt2 is not None and nxt2.kind == "dot":
                    self._label(tk)
                    i += 3
                    continue
            at_start = False
            plen = self._phrase_len(i)
            if plen:
                text = " ".join(x.text for x in self.t[i : i + plen])
                if self.frames and self.frames[-1].kind == "PHRASE":
                    self.frames.pop()
                self.frames.append(Frame("PHRASE", text, [], tk.line))
                i += plen
                continue
            handler = self._handlers.get(u) if tk.kind == "word" else None
            if handler is not None:
                i = handler(self, i)
                continue
            if tk.kind == "word" and u.startswith("END-"):
                self._close_scope(u)
            i += 1
        return self

    def _label(self, tk: Tok) -> None:
        self.para = tk.text
        self.paragraphs.setdefault(tk.upper, tk.line)
        self.frames.clear()

    def _close_scope(self, u: str) -> None:
        target = {"END-IF": "IF", "END-EVALUATE": "EVAL", "END-PERFORM": "LOOP"}.get(u)
        if target:
            if any(f.kind == target for f in self.frames):
                while self.frames:
                    if self.frames.pop().kind == target:
                        break
        elif self.frames and self.frames[-1].kind == "PHRASE":
            self.frames.pop()

    # -- statement handlers
    def _if(self, i: int) -> int:
        line = self.t[i].line
        cond, i = self._collect(i + 1, frozenset({"THEN"}))
        tk = self._tok(i)
        if tk is not None and tk.upper == "THEN":
            i += 1
        self.frames.append(Frame("IF", render(cond), cond, line))
        return i

    def _else(self, i: int) -> int:
        while self.frames and not (self.frames[-1].kind == "IF" and self.frames[-1].branch == "then"):
            self.frames.pop()
        if self.frames:
            self.frames[-1].branch = "else"
        return i + 1

    def _evaluate(self, i: int) -> int:
        line = self.t[i].line
        subj, i = self._collect(i + 1)
        self.frames.append(Frame("EVAL", render(subj), subj, line))
        return i

    def _when(self, i: int) -> int:
        idx = next((k for k in range(len(self.frames) - 1, -1, -1) if self.frames[k].kind == "EVAL"), None)
        start, line = i, self.t[i].line
        vals, i = self._collect(i + 1)
        if idx is None:  # SEARCH ... WHEN: not modelled
            return i
        del self.frames[idx + 1 :]
        ev = self.frames[idx]
        text = render(vals)
        if text.upper() == "OTHER":
            ev.prev_conds.extend(ev.cur_conds)
            ev.cur_conds, ev.is_other = [], True
        else:
            cond = _when_to_cond(ev.text, text)
            if ev.last_when_end == start and not ev.is_other:
                ev.cur_conds.append(cond)
                ev.when_toks = ev.when_toks + vals
            else:
                ev.prev_conds.extend(ev.cur_conds)
                ev.cur_conds, ev.when_toks = [cond], list(vals)
        ev.when_line = line
        ev.last_when_end = i
        return i

    def _perform(self, i: int) -> int:
        line = self.t[i].line
        t1, t2 = self._tok(i + 1), self._tok(i + 2)
        if t1 is None:
            return i + 1
        inline = (
            t1.upper in ("UNTIL", "VARYING", "WITH", "TEST", "FOREVER") or t1.upper in VERBS
            or t1.kind != "word" or (t2 is not None and t2.upper == "TIMES")
        )
        if inline:
            hdr, i = self._collect(i + 1)
            self.frames.append(Frame("LOOP", ("PERFORM " + render(hdr)).strip(), hdr, line))
            return i
        target, i = t1.text, i + 2
        thru = None
        tk = self._tok(i)
        if tk is not None and tk.upper in ("THRU", "THROUGH") and self._tok(i + 1) is not None:
            thru, i = self.t[i + 1].text, i + 2
        rest, i = self._collect(i)
        conds = self._context()[0]
        self.calls.append(Call(self.para, target, thru, line, render(rest), conds))
        return i

    def _compute(self, i: int) -> int:
        start = self.t[i]
        i += 1
        lhs: list[Tok] = []
        while i < self.n and not (self.t[i].text == "=" or self.t[i].upper == "EQUAL") and not self._boundary(self.t[i], frozenset()):
            lhs.append(self.t[i])
            i += 1
        if i >= self.n or self._boundary(self.t[i], frozenset()):
            self.skipped.append((start.line, "COMPUTE without '='"))
            return i
        i += 1
        expr, i = self._collect(i, ARITH_STOPS)
        rounded = any(x.upper == "ROUNDED" for x in lhs)
        targets = operands([x for x in lhs if x.upper not in ROUNDING_NOISE])
        if not expr or not targets:
            self.skipped.append((start.line, "COMPUTE with empty target or expression"))
            return i
        end_line = max(x.line for x in lhs + expr)
        for tgt in targets:
            self._emit("calculation", "COMPUTE", tgt, render(expr), expr, start.line, end_line,
                       ["ROUNDED"] if rounded else [])
        return i

    def _arith(self, i: int) -> int:
        start = self.t[i]
        verb = start.upper
        body, i = self._collect(i + 1, ARITH_STOPS)
        if not body:
            return i
        end_line = max(x.line for x in body)
        if any(x.upper in ("CORRESPONDING", "CORR") for x in body):
            self.skipped.append((start.line, f"{verb} CORRESPONDING not modelled"))
            return i
        rounded = any(x.upper == "ROUNDED" for x in body)
        clean = [x for x in body if x.upper not in ROUNDING_NOISE]
        segs: dict[str, list[Tok]] = {"": []}
        key = ""
        for x in clean:
            if x.kind == "word" and x.upper in ("TO", "FROM", "BY", "INTO", "GIVING", "REMAINDER"):
                key = x.upper
                segs.setdefault(key, [])
            else:
                segs[key].append(x)
        ops = [render(o) for o in operands(segs[""])]
        giving = operands(segs.get("GIVING", []))
        plan: list[tuple[list[Tok], str]] = []  # (target tokens, expression)
        if not ops:
            self.skipped.append((start.line, f"{verb} without operands"))
            return i
        if verb == "ADD":
            to = operands(segs.get("TO", []))
            if giving:
                plan = [(g, " + ".join(ops + [render(o) for o in to])) for g in giving]
            else:
                plan = [(t, " + ".join([render(t)] + ops)) for t in to]
        elif verb == "SUBTRACT":
            frm = operands(segs.get("FROM", []))
            if giving and frm:
                plan = [(g, " - ".join([render(frm[0])] + ops)) for g in giving]
            else:
                plan = [(t, " - ".join([render(t)] + ops)) for t in frm]
        elif verb == "MULTIPLY":
            by = operands(segs.get("BY", []))
            if giving and by:
                plan = [(g, f"{ops[0]} * {render(by[0])}") for g in giving]
            else:
                plan = [(b, f"{ops[0]} * {render(b)}") for b in by]
        elif verb == "DIVIDE":
            into, by = operands(segs.get("INTO", [])), operands(segs.get("BY", []))
            if into:
                dividend, divisor = render(into[0]), ops[0]
                plan = [(g, f"{dividend} / {divisor}") for g in giving] if giving else [(t, f"{render(t)} / {divisor}") for t in into]
            elif by:
                dividend, divisor = ops[0], render(by[0])
                plan = [(g, f"{dividend} / {divisor}") for g in giving]
            else:
                dividend = divisor = ""
            for r in operands(segs.get("REMAINDER", [])):
                plan.append((r, f"REMAINDER OF ({dividend} / {divisor})"))
        if not plan:
            self.skipped.append((start.line, f"{verb} form not recognised"))
            return i
        for tgt, expr in plan:
            self._emit("calculation", verb, tgt, expr, body, start.line, end_line, ["ROUNDED"] if rounded else [])
        return i

    def _move(self, i: int) -> int:
        start = self.t[i]
        body, i = self._collect(i + 1)
        if not body:
            return i
        end_line = max(x.line for x in body)
        if body[0].upper in ("CORRESPONDING", "CORR"):
            self.skipped.append((start.line, "MOVE CORRESPONDING not modelled"))
            return i
        cut = next((k for k, x in enumerate(body) if x.upper == "TO"), None)
        if cut is None:
            return i
        src, targets = body[:cut], operands(body[cut + 1 :])
        literal = is_literal(src)
        conds = self._context()[0]
        is_function = bool(src) and src[0].upper == "FUNCTION"
        init_only = literal and len(src) == 1 and src[0].upper in INIT_FIGURATIVE
        for tgt in targets:
            if (literal and not init_only) or conds or is_function:
                self._emit("assignment", "MOVE", tgt, render(src), src, start.line, end_line)
            else:
                self._mark_modified(tgt)
                self.data_moves += 1
        return i

    def _set(self, i: int) -> int:
        start = self.t[i]
        body, i = self._collect(i + 1)
        if not body:
            return i
        end_line = max(x.line for x in body)
        cut = next((k for k, x in enumerate(body) if x.upper == "TO"), None)
        if cut is None:  # SET idx UP BY n, etc.
            return i
        value = body[cut + 1 :]
        for tgt in operands(body[:cut]):
            item = self.dd.get(tgt[0].upper)
            if value and value[0].upper == "TRUE" and item is not None and item.level == 88 and item.parent:
                conds, ctoks, clines, loops = self._context()
                self.findings.append(ProcFinding(
                    kind="assignment", verb="SET", target=item.parent,
                    expression=item.values[0] if item.values else "?", expr_toks=[], conditions=conds,
                    cond_toks=ctoks, condition_lines=clines, loops=loops,
                    start_line=start.line, end_line=end_line, paragraph=self.para, anchor=tgt[0].text,
                    notes=[f"SET {tgt[0].text} TO TRUE (88-level on {item.parent})"],
                ))
                self._mark_modified([Tok("word", item.parent, tgt[0].line)])
            elif is_literal(value):
                self._emit("assignment", "SET", tgt, render(value), value, start.line, end_line)
        return i

    def _exec(self, i: int) -> int:
        start = i
        while i < self.n and self.t[i].upper != "END-EXEC":
            i += 1
        sql = " ".join(x.text for x in self.t[start:i]).replace(" . ", ".")
        self.sql_blocks += 1
        for m in re.finditer(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+([A-Za-z0-9_$#.]+)", sql, re.I):
            if m.group(1).upper() not in ("SQLCA", "SQL"):
                self.sql_tables.add(m.group(1).upper())
        return i + 1

    def _copy(self, i: int) -> int:
        tk = self._tok(i + 1)
        if tk is not None:
            self.copies.append(tk.text.strip("'\"").upper())
        _, i = self._collect(i + 1)
        return i

    def _generic(self, i: int) -> int:
        _, i = self._collect(i + 1, ARITH_STOPS)
        return i

    _handlers = {
        "IF": _if, "ELSE": _else, "EVALUATE": _evaluate, "WHEN": _when, "PERFORM": _perform,
        "COMPUTE": _compute, "ADD": _arith, "SUBTRACT": _arith, "MULTIPLY": _arith, "DIVIDE": _arith,
        "MOVE": _move, "SET": _set, "EXEC": _exec, "COPY": _copy,
    }
    for _verb in VERBS:
        _handlers.setdefault(_verb, _generic)
    del _verb


def inputs_of(toks: list[Tok], dd: dict[str, DataItem]) -> list[str]:
    """Identifiers a statement reads; 88-level names resolve to the item they test."""
    out: list[str] = []
    skip_next = False
    for tk in toks:
        if skip_next:
            skip_next = False
            continue
        if tk.kind != "word":
            continue
        u = tk.upper
        if u == "FUNCTION":
            skip_next = True
            continue
        if u in KEYWORDS or u.isdigit():
            continue
        item = dd.get(u)
        name = item.parent if item is not None and item.level == 88 and item.parent else tk.text
        if name.upper() not in (o.upper() for o in out):
            out.append(name)
    return out
