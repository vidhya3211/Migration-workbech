"""Language-aware canonicalisation of rule expressions/conditions into the evaluator's syntax.

    COBOL  COMPUTE WS-MONTHLY-PREMIUM ROUNDED = (WS-BASE-PREMIUM / 12) + WS-ADMIN-FEE
        -> ROUND((BASE_PREMIUM / 12) + ADMIN_FEE, 2)
    PHP    round($basePremium / 12, (int) $this->roundingPrecision) + self::ADMIN_FEE
        -> ROUND(BASE_PREMIUM / 12, ROUNDING_PRECISION) + ADMIN_FEE

The canonical form is what makes source and target comparable - and provable - regardless of language.
"""
from __future__ import annotations

import re

from reasoning.deterministic.expressions import ExpressionError, parse
from reasoning.deterministic.naming import canonical_name


class CanonicalError(ValueError):
    pass


def _check(out: str) -> str:
    try:
        parse(out)
    except ExpressionError as exc:
        raise CanonicalError(f"{exc} in canonical form {out!r}") from exc
    return out


# ---------------------------------------------------------------------------------------------- COBOL
_COBOL_TOK = re.compile(r"""(?P<ws>\s+)|(?P<str>'(?:[^']|'')*'|"(?:[^"]|"")*")|(?P<num>\d+(?:\.\d+)?|\.\d+)|
    (?P<word>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)|(?P<op>\*\*|>=|<=|<>|[-+*/=<>(),])|(?P<other>.)""", re.X)
_COBOL_WORD_OPS = [
    (("GREATER", "THAN", "OR", "EQUAL", "TO"), ">="), (("LESS", "THAN", "OR", "EQUAL", "TO"), "<="),
    (("GREATER", "OR", "EQUAL"), ">="), (("LESS", "OR", "EQUAL"), "<="),
    (("NOT", "GREATER", "THAN"), "<="), (("NOT", "LESS", "THAN"), ">="), (("NOT", "EQUAL", "TO"), "!="),
    (("NOT", "EQUAL"), "!="), (("NOT", "="), "!="), (("GREATER", "THAN"), ">"), (("LESS", "THAN"), "<"),
    (("EQUAL", "TO"), "="), (("EQUALS",), "="), (("EQUAL",), "="), (("GREATER",), ">"), (("LESS",), "<"),
]
_FIGURATIVE = {"ZERO": "0", "ZEROS": "0", "ZEROES": "0", "SPACE": "' '", "SPACES": "' '", "TRUE": "TRUE",
               "FALSE": "FALSE", "HIGH-VALUE": "'~'", "HIGH-VALUES": "'~'", "LOW-VALUE": "''", "LOW-VALUES": "''"}
_COBOL_NOISE = {"IS", "THEN", "ROUNDED", "FUNCTION", "OF", "IN"}


def cobol(expr: str, *, condition_names: dict[str, str] | None = None, rounded_scale: int | None = None,
          truncate_scale: int | None = None, _depth: int = 0) -> str:
    condition_names = {k.upper(): v for k, v in (condition_names or {}).items()}
    expr = re.sub(r"[A-Za-z0-9-]+\s*=\s*ANY\b", "TRUE", expr, flags=re.I)   # EVALUATE ... WHEN ANY
    words: list[tuple[str, str]] = []
    for m in _COBOL_TOK.finditer(expr):
        if m.lastgroup in ("ws",):
            continue
        if m.lastgroup == "other":
            raise CanonicalError(f"unsupported character {m.group()!r}")
        words.append((m.lastgroup, m.group()))
    out: list[str] = []
    i = 0
    while i < len(words):
        kind, text = words[i]
        up = text.upper()
        matched = False
        for seq, op in _COBOL_WORD_OPS:
            if tuple(w[1].upper() for w in words[i:i + len(seq)]) == seq:
                out.append(op)
                i += len(seq)
                matched = True
                break
        if matched:
            continue
        if kind == "word" and up == "REMAINDER" and i + 1 < len(words) and words[i + 1][1].upper() == "OF":
            out.append("MOD")
            i += 2
            continue
        if kind == "word" and up in _COBOL_NOISE and up not in ("IN",):
            i += 1
            continue
        if kind == "word" and up in ("AND", "OR", "NOT"):
            out.append(up)
        elif kind == "word" and up in _FIGURATIVE:
            out.append(_FIGURATIVE[up])
        elif kind == "word" and up == "ANY":
            out.append("TRUE")
        elif kind == "word" and up in condition_names and _depth < 3:
            out.append("(" + cobol(condition_names[up], condition_names=condition_names, _depth=_depth + 1) + ")")
        elif kind == "word" and re.fullmatch(r"\d+", up):
            out.append(up)
        elif kind == "word":
            if i + 1 < len(words) and words[i + 1][1] == "(" and up in ("ROUND", "ABS", "MAX", "MIN", "MOD", "INTEGER",
                                                                      "INTEGER-PART", "NUMVAL", "SUM"):
                out.append({"INTEGER-PART": "TRUNC", "INTEGER": "TRUNC", "NUMVAL": "NVL"}.get(up, up))
            else:
                out.append(canonical_name(text))
        elif kind == "str":
            out.append("'" + text[1:-1].replace('""', '"') + "'")
        elif text == "<>":
            out.append("!=")
        else:
            out.append(text)
        i += 1
    body = _join(out)
    # "X = 'A' 'B'" style multi-value comparisons from 88-levels are written as IN lists by the agent.
    if rounded_scale is not None:
        body = f"ROUND({body}, {rounded_scale})"
    elif truncate_scale is not None:
        body = f"TRUNC({body}, {truncate_scale})"
    return _check(body)


def _join(parts: list[str]) -> str:
    s = " ".join(parts)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r"([A-Z_][A-Z0-9_]*)\s+\(", lambda m: m.group(1) + "(" if m.group(1) in _FUNCS else m.group(0), s)
    return s


_FUNCS = {"ROUND", "TRUNC", "ABS", "MIN", "MAX", "MOD", "NVL", "COALESCE"}


# ---------------------------------------------------------------------------------------------- PHP
_PHP_FUNCS = {"round": "ROUND", "floor": "TRUNC", "abs": "ABS", "min": "MIN", "max": "MAX", "intdiv": "TRUNC",
              "fmod": "MOD", "bcadd": None}


def php(expr: str, *, owner_class: str | None = None) -> str:
    from tools.parsers.php import tokenize as ptok
    toks = ptok("<?php " + expr + ";")
    toks = [t for t in toks if t.text != ";"]
    out = _php_tokens(toks, owner_class)
    return _check(_join(out))


def _php_tokens(toks: list, owner_class: str | None) -> list[str]:  # noqa: C901 - a token-level translator
    out: list[str] = []
    i = 0
    n = len(toks)
    while i < n:
        tk = toks[i]
        text, low = tk.text, tk.text.lower()
        nxt = toks[i + 1] if i + 1 < n else None
        # casts: (int) (float) (string) (bool)
        if text == "(" and nxt is not None and nxt.text.lower() in ("int", "float", "string", "bool", "array") \
                and i + 2 < n and toks[i + 2].text == ")":
            i += 3
            continue
        if tk.kind == "var":
            j = i + 1
            last = text
            called = False
            while j + 1 < n and toks[j].text in ("->", "?->") and toks[j + 1].kind == "ident":
                last = toks[j + 1].text
                j += 2
                if j < n and toks[j].text == "(":
                    j = _skip_parens(toks, j)
                    called = True
                    break
            name = canonical_name(last, strip_verbs=called)
            out.append(name)
            i = j
            continue
        if tk.kind == "ident" and nxt is not None and nxt.text == "::" and i + 2 < n:
            member = toks[i + 2].text
            j = i + 3
            called = j < n and toks[j].text == "("
            if called:
                j = _skip_parens(toks, j)
            out.append(canonical_name(member, strip_verbs=called))
            i = j
            continue
        if tk.kind == "ident" and text in ("NOT", "AND", "OR"):   # connectives added by the parser's condition stack
            out.append(text)
            i += 1
            continue
        if tk.kind == "ident" and low == "match" and nxt is not None and nxt.text == "(":
            i = _php_match(toks, i, out, owner_class)
            continue
        if tk.kind == "ident" and low == "in_array" and nxt is not None and nxt.text == "(":
            args, j = _args(toks, i + 1)
            needle = _join(_php_tokens(args[0], owner_class))
            hay = [t for t in args[1] if t.kind in ("str", "num")]
            items = ", ".join("'" + t.text[1:-1] + "'" if t.kind == "str" else t.text for t in hay)
            out.append(f"({needle} IN ({items}))")
            i = j
            continue
        if tk.kind == "ident" and nxt is not None and nxt.text == "(":
            fn = _PHP_FUNCS.get(low)
            if fn is None:
                raise CanonicalError(f"unsupported PHP function {text}()")
            out.append(fn)
            i += 1
            continue
        if tk.kind == "ident" and low in ("true", "false", "null"):
            out.append(low.upper())
        elif tk.kind == "ident":
            out.append(canonical_name(text))
        elif tk.kind == "num":
            out.append(text.replace("_", ""))
        elif tk.kind == "str":
            out.append("'" + text[1:-1].replace("\\'", "''") + "'")
        elif text in ("===", "=="):
            out.append("=")
        elif text in ("!==", "!=", "<>"):
            out.append("!=")
        elif text == "&&":
            out.append("AND")
        elif text == "||":
            out.append("OR")
        elif text == "!":
            out.append("NOT")
        elif text in ("+", "-", "*", "/", "**", "<", "<=", ">", ">=", "(", ")", ","):
            out.append(text)
        elif text == "[":          # array index: a table lookup, written like a COBOL subscript
            out.append("(")
        elif text == "]":
            out.append(")")
        elif text == "??":
            raise CanonicalError("null-coalescing is not modelled")
        else:
            raise CanonicalError(f"unsupported PHP token {text!r}")
        i += 1
    return out


def _skip_parens(toks: list, j: int) -> int:
    depth = 0
    while j < len(toks):
        depth += {"(": 1, ")": -1}.get(toks[j].text, 0)
        j += 1
        if depth == 0:
            break
    return j


def _args(toks: list, j: int) -> tuple[list[list], int]:
    """toks[j] == '(' -> list of argument token lists, index after ')'."""
    args: list[list] = [[]]
    depth = 0
    k = j
    while k < len(toks):
        t = toks[k].text
        if t in ("(", "["):
            depth += 1
            if depth == 1 and t == "(":
                k += 1
                continue
        elif t in (")", "]"):
            depth -= 1
            if depth == 0:
                return args, k + 1
        elif t == "," and depth == 1:
            args.append([])
            k += 1
            continue
        args[-1].append(toks[k])
        k += 1
    return args, k


def _php_match(toks: list, i: int, out: list[str], owner_class: str | None) -> int:
    subj_args, j = _args(toks, i + 1)
    subject = _join(_php_tokens(subj_args[0], owner_class))
    if j >= len(toks) or toks[j].text != "{":
        raise CanonicalError("malformed match expression")
    depth, k, arms, cur = 0, j, [], []
    while k < len(toks):
        t = toks[k].text
        if t in ("{", "(", "["):
            depth += 1
            if depth == 1 and t == "{":
                k += 1
                continue
        elif t in ("}", ")", "]"):
            depth -= 1
            if depth == 0:
                break
        if t == "," and depth == 1:
            arms.append(cur)
            cur = []
        else:
            cur.append(toks[k])
        k += 1
    if cur:
        arms.append(cur)
    parts, default = [], None
    for arm in arms:
        arrow = next((x for x, t in enumerate(arm) if t.text == "=>"), None)
        if arrow is None:
            continue
        cond_t, val_t = arm[:arrow], arm[arrow + 1:]
        value = _join(_php_tokens(val_t, owner_class))
        if len(cond_t) == 1 and cond_t[0].text.lower() == "default":
            default = value
            continue
        cond = _join(_php_tokens(cond_t, owner_class))
        parts.append(f"WHEN {cond} THEN {value}" if subject == "TRUE" else f"WHEN {subject} = {cond} THEN {value}")
    out.append("(CASE " + " ".join(parts) + (f" ELSE {default}" if default else "") + " END)")
    return k + 1


# ---------------------------------------------------------------------------------------------- PL/SQL
def plsql(expr: str, aliases: dict[str, str] | None = None) -> str:
    """``aliases`` maps local variables to the columns they were SELECTed INTO from (v_fund -> fund_value)."""
    from tools.parsers.plsql import tokenize as qtok
    aliases = {k.upper(): v for k, v in (aliases or {}).items()}
    out: list[str] = []
    for tk in qtok(expr):
        up = tk.text.upper()
        if tk.kind == "word":
            if up in ("AND", "OR", "NOT", "IN", "CASE", "WHEN", "THEN", "ELSE", "END", "NULL", "TRUE", "FALSE", "IS"):
                out.append(up if up != "IS" else "")
            elif up in _FUNCS or up in ("TRUNC", "GREATEST", "LEAST"):
                out.append({"GREATEST": "MAX", "LEAST": "MIN"}.get(up, up))
            else:
                out.append(canonical_name(aliases.get(up, tk.text).split(".")[-1]))
        elif tk.kind == "str":
            out.append(tk.text)
        elif tk.text == "<>":
            out.append("!=")
        elif tk.text == "||":
            raise CanonicalError("string concatenation is not modelled")
        else:
            out.append(tk.text)
    return _check(_join([p for p in out if p]))


# ---------------------------------------------------------------------------------------------- Excel
def excel(formula: str, cell_labels: dict[str, str]) -> str:
    body = formula[1:] if formula.startswith("=") else formula

    def ref(m: re.Match[str]) -> str:
        label = cell_labels.get(m.group(1).replace("$", ""))
        if label is None:
            raise CanonicalError(f"cell {m.group(1)} has no label")
        return canonical_name(label)
    body = re.sub(r"(?<![A-Za-z_])(\$?[A-Z]{1,3}\$?\d+)(?![A-Za-z0-9_(])", ref, body)
    body = body.replace("<>", "!=").replace("^", "**")
    body = re.sub(r"\bIF\(", "_IF(", body, flags=re.I)
    while "_IF(" in body:   # IF(c, a, b) -> CASE WHEN c THEN a ELSE b END (innermost first)
        m = re.search(r"_IF\(([^()]*(?:\([^()]*\)[^()]*)*)\)", body)
        if not m:
            raise CanonicalError("unsupported nested IF")
        args = _split_commas(m.group(1))
        if len(args) != 3:
            raise CanonicalError("IF needs three arguments")
        body = body[:m.start()] + f"(CASE WHEN {args[0]} THEN {args[1]} ELSE {args[2]} END)" + body[m.end():]
    return _check(body)


def _split_commas(s: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in s:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    parts.append(cur.strip())
    return parts


def generic_sql(expr: str) -> str:
    return plsql(expr)
