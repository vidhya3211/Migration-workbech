"""Deterministic document reader: text extraction with exact locators + rule-assertion patterns.

Formats: Markdown / text / HTML (line locators), DOCX (paragraph = line), PDF (page + line). Sections come
from headings. Assertions ("an administration charge of GBP 25.00", "aged 60 or over", "5% penalty")
are extracted by explainable patterns; sentences with figures that no pattern understands are returned
as *unparsed claims* so the Document agent can route them to the LLM (or a human) - never dropped.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class DocLine:
    page: int | None
    line: int          # 1-based line within the decoded text (or within the page for PDFs)
    text: str
    section: str | None


@dataclass
class Span:
    """A sentence located in the document: page + inclusive line range."""
    page: int | None
    line_start: int
    line_end: int
    section: str | None


@dataclass
class Assertion:
    name: str
    expression: str
    value: str | None
    operator: str | None
    condition: str | None
    span: Span
    sentence: str
    anchor: str
    confidence: float
    pattern: str
    notes: list[str] = field(default_factory=list)


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []

    def handle_data(self, data: str) -> None:
        self.out.append(data)


def _docx_paragraphs(data: bytes) -> list[tuple[str, bool]]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", "replace")
    paras = []
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, re.S))
        heading = bool(re.search(r'<w:pStyle w:val="(Heading|Title)', p))
        paras.append((re.sub(r"&amp;", "&", re.sub(r"&lt;", "<", re.sub(r"&gt;", ">", text))), heading))
    return paras


def lines_for(fmt: str, text: str | None, data: bytes) -> list[DocLine]:
    out: list[DocLine] = []
    section: str | None = None
    if fmt == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        for pno, page in enumerate(reader.pages, 1):
            for lno, raw in enumerate((page.extract_text() or "").split("\n"), 1):
                if raw.strip().isupper() and 3 < len(raw.strip()) < 80:
                    section = raw.strip()
                out.append(DocLine(pno, lno, raw, section))
        return out
    if fmt == "docx":
        for lno, (raw, heading) in enumerate(_docx_paragraphs(data), 1):
            if heading:
                section = raw.strip()
            out.append(DocLine(None, lno, raw, section))
        return out
    assert text is not None
    for lno, raw in enumerate(text.split("\n"), 1):
        stripped = raw.strip()
        if fmt in ("md", "txt") and stripped.startswith("#"):
            section = stripped.lstrip("#").strip()
        elif fmt == "html":
            m = re.match(r"\s*<h[1-6][^>]*>(.*?)</h[1-6]>", raw, re.I)
            if m:
                section = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        out.append(DocLine(None, lno, raw, section))
    return out


def plain(line: str, fmt: str) -> str:
    if fmt == "html":
        p = _HtmlText()
        p.feed(line)
        return "".join(p.out)
    return line


_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "first": 1, "second": 2, "third": 3}
_NUM = r"(\d+(?:\.\d+)?)"
_MONEY_NAME = r"((?:[a-z]+\s){0,3}?(?:fee|charge|premium|value|bonus|discount|loading|rate|amount))"

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("named_amount", re.compile(rf"\b{_MONEY_NAME}\s+of\s+(?:GBP\s?|£)?{_NUM}\s*(%)?", re.I)),
    ("pays_percent_more", re.compile(rf"\b([a-z]+s)\s+pay\s+{_NUM}%\s+more\b", re.I)),
    ("age_threshold", re.compile(rf"\baged\s+{_NUM}\s+or\s+(over|more|above|under|less|below)\b", re.I)),
    ("percent_named", re.compile(rf"\b{_NUM}%\s+((?:[a-z]+\s){{0,2}}?(?:penalty|loading|discount|charge|bonus))\b", re.I)),
    ("incur_percent", re.compile(rf"\bincur\s+{_NUM}%", re.I)),
]
_STOP = {"a", "an", "the", "fixed", "is", "are", "of", "with", "at", "on", "for", "to", "and", "or", "every", "each"}
_VERBS = {"attract", "attracts", "incur", "incurs", "pay", "pays", "apply", "applies", "add", "adds", "added",
          "charged", "receive", "receives", "have", "has", "get", "gets"}


def _name(phrase: str) -> str:
    """Noun phrase -> name: drop articles and verbs, keep at most the last three words."""
    words = re.findall(r"[a-z]+", phrase.lower())
    for k in range(len(words) - 1, -1, -1):   # cut at the last verb: "attract an age loading" -> "age loading"
        if words[k] in _VERBS:
            words = words[k + 1:]
            break
    return " ".join([w for w in words if w not in _STOP][-3:])


def _years_condition(sentence: str) -> str | None:
    s = sentence.lower()
    m = re.search(r"first\s+(\w+)\s+(?:policy\s+)?years", s)
    if m and (n := _WORDNUM.get(m.group(1)) or (int(m.group(1)) if m.group(1).isdigit() else None)):
        return f"YEARS_IN_FORCE < {n}"
    m = re.search(r"years\s+(\w+)\s+and\s+(\w+)", s)
    if m:
        a, b = (_WORDNUM.get(x) or (int(x) if x.isdigit() else None) for x in m.groups())
        if a and b:
            return f"{a} <= YEARS_IN_FORCE < {b + 1}"
    return None


def paragraphs(lines: list[DocLine], fmt: str) -> list[list[DocLine]]:
    """Consecutive non-blank, non-heading lines on the same page form a paragraph."""
    out: list[list[DocLine]] = []
    cur: list[DocLine] = []
    for dl in lines:
        text = plain(dl.text, fmt).strip()
        if not text or text.startswith("#") or (cur and cur[-1].page != dl.page):
            if cur:
                out.append(cur)
            cur = [] if not text or text.startswith("#") else [dl]
            continue
        cur.append(dl)
    if cur:
        out.append(cur)
    return out


def sentences(para: list[DocLine], fmt: str) -> list[tuple[str, Span]]:
    """Split a paragraph into sentences, mapping each back to the exact lines it spans."""
    joined, owners = "", []   # owners[i] = index of the line owning char i
    for k, dl in enumerate(para):
        piece = plain(dl.text, fmt).strip()
        if joined:
            joined += " "
            owners.append(k)
        joined += piece
        owners.extend([k] * len(piece))
    out: list[tuple[str, Span]] = []
    bounds = [m.end() for m in re.finditer(r"[.;!?](?=\s|$)", joined)]   # "25.00" is not a boundary
    if not bounds or bounds[-1] != len(joined):
        bounds.append(len(joined))
    prev = 0
    for end in bounds:
        chunk = joined[prev:end]
        s = chunk.strip()
        if s:
            start = prev + (len(chunk) - len(chunk.lstrip()))
            first, last = para[owners[start]], para[owners[min(end - 1, len(owners) - 1)]]
            out.append((s, Span(first.page, first.line, last.line, first.section)))
        prev = end
    return out


def extract_assertions(lines: list[DocLine], fmt: str) -> tuple[list[Assertion], list[Span]]:
    """Returns (assertions, spans of sentences with figures that no pattern understood)."""
    out: list[Assertion] = []
    unparsed: list[Span] = []
    for para in paragraphs(lines, fmt):
        last_subject: str | None = None
        for sentence, span in sentences(para, fmt):
            found = False
            for pname, pat in PATTERNS:
                for m in pat.finditer(sentence):
                    a = _build(pname, m, sentence, span, last_subject)
                    if a:
                        out.append(a)
                        found = True
            named = [a for a in out if a.span == span and a.pattern in ("named_amount", "percent_named")]
            if named:
                last_subject = named[-1].name
            if not found and re.search(r"\d", sentence):
                unparsed.append(span)
    return _dedupe(out), unparsed


def _build(pname: str, m: re.Match[str], sentence: str, dl: Span, last_subject: str | None) -> Assertion | None:
    if pname == "named_amount":
        name = _name(m.group(1))
        pct = m.group(3) == "%"
        value = str(float(m.group(2)) / 100) if pct else m.group(2)
        notes = []
        if pct and "loading" in name:
            value = str(1 + float(m.group(2)) / 100)
            notes.append(f"{m.group(2)}% loading expressed as factor {value}")
        return Assertion(name, value, value, "=", None, dl, sentence, m.group(2), 0.8, pname, notes)
    if pname == "pays_percent_more":
        who = _name(m.group(1)).rstrip("s")
        factor = str(1 + float(m.group(2)) / 100)
        return Assertion(f"{who} loading", factor, factor, "=", None, dl, sentence, m.group(2), 0.75, pname,
                         [f"'{m.group(2)}% more' expressed as factor {factor}"])
    if pname == "age_threshold":
        op = {"over": ">=", "more": ">=", "above": ">=", "under": "<", "less": "<", "below": "<"}[m.group(2).lower()]
        subject = _subject_in(sentence) or last_subject or "age"
        return Assertion(f"{subject} threshold", m.group(1), m.group(1), op, f"AGE {op} {m.group(1)}", dl, sentence,
                         m.group(1), 0.75, pname, [f"'{m.group(0)}' read as AGE {op} {m.group(1)}"])
    if pname in ("percent_named", "incur_percent"):
        subject = _name(m.group(2)) if pname == "percent_named" else (last_subject or "penalty")
        rate = str(float(m.group(1)) / 100)
        cond = _years_condition(sentence)
        name = "penalty rate" if "penalty" in subject else subject
        return Assertion(name, rate, rate, "=", cond, dl, sentence, m.group(1), 0.7 if cond else 0.6, pname,
                         [f"{m.group(1)}% expressed as rate {rate}"])
    return None


def _subject_in(sentence: str) -> str | None:
    m = re.search(r"\b((?:[a-z]+\s)?(?:loading|discount|charge|fee|penalty))\b", sentence, re.I)
    return _name(m.group(1)) if m else None


def _dedupe(items: list[Assertion]) -> list[Assertion]:
    seen: set[tuple] = set()
    out = []
    for a in items:
        key = (a.name, a.value, a.condition, a.span.line_start, a.span.page)
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out
