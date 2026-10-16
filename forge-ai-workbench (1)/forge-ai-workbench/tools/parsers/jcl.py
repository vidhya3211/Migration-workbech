"""Deterministic JCL reader: JOB card, EXEC steps (PGM/PROC, PARM, COND), DD statements, continuations."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class JclStatement:
    name: str | None
    op: str
    operands: str
    start_line: int
    end_line: int


@dataclass
class JclStep:
    name: str | None
    program: str | None
    proc: str | None
    parm: str | None
    cond: str | None
    start_line: int
    end_line: int
    dds: list[dict] = field(default_factory=list)


@dataclass
class JclJob:
    name: str | None
    start_line: int
    end_line: int
    params: dict[str, str]
    steps: list[JclStep]


_STMT = re.compile(r"^//(\S*)\s+(\S+)\s*(.*)$")


def statements(text: str) -> list[JclStatement]:
    out: list[JclStatement] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i][:72]
        if raw.startswith("//*") or not raw.startswith("//") or raw.strip() == "//":
            i += 1
            continue
        m = _STMT.match(raw.rstrip())
        if not m:
            i += 1
            continue
        name, op, operands = m.group(1) or None, m.group(2).upper(), m.group(3).strip()
        start = i + 1
        # continuation: operand field ends with a comma -> next // line continues it
        while operands.split(" ")[0].endswith(",") and i + 1 < len(lines) and lines[i + 1].startswith("//") \
                and not lines[i + 1].startswith("//*"):
            i += 1
            operands = operands.split(" ")[0] + lines[i][2:72].strip()
        out.append(JclStatement(name, op, operands.split(" ")[0] if op != "JOB" else operands, start, i + 1))
        i += 1
    return out


def _params(operands: str) -> dict[str, str]:
    out: dict[str, str] = {}
    depth, cur, quote = 0, "", False
    parts: list[str] = []
    for ch in operands:
        if ch == "'":
            quote = not quote
        if not quote:
            depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0 and not quote:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip().upper()] = v.strip()
        elif p.strip():
            out.setdefault("_POSITIONAL", p.strip())
    return out


def parse(text: str) -> list[JclJob]:
    jobs: list[JclJob] = []
    job: JclJob | None = None
    step: JclStep | None = None
    for st in statements(text):
        if st.op == "JOB":
            job = JclJob(st.name, st.start_line, st.end_line, _params(st.operands), [])
            jobs.append(job)
            step = None
        elif st.op == "EXEC":
            if job is None:
                job = JclJob(None, st.start_line, st.end_line, {}, [])
                jobs.append(job)
            p = _params(st.operands)
            program = p.get("PGM")
            proc = p.get("PROC") or (p.get("_POSITIONAL") if not program else None)
            parm = p.get("PARM")
            step = JclStep(st.name, program, proc, parm.strip("'") if parm else None, p.get("COND"),
                           st.start_line, st.end_line)
            job.steps.append(step)
            job.end_line = st.end_line
        elif st.op == "DD" and step is not None:
            p = _params(st.operands)
            step.dds.append({"ddname": st.name, "dsn": p.get("DSN") or p.get("DSNAME"), "disp": p.get("DISP"),
                             "line": st.start_line})
            step.end_line = st.end_line
            if job is not None:
                job.end_line = st.end_line
    return jobs


def parm_assignments(parm: str) -> list[tuple[str, str]]:
    """``PARM='FEE=25,MODE=LIVE'`` -> [("FEE", "25"), ("MODE", "LIVE")]."""
    return [(k.strip().upper(), v.strip()) for k, v in re.findall(r"([A-Za-z][A-Za-z0-9_-]*)\s*=\s*([^,]+)", parm)]
