"""PII detection and redaction (UK-centric: NI numbers, sort codes, postcodes, phones, cards, emails, DOBs).

Used before anything reaches an LLM, telemetry, Headroom telemetry or MCP logs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from guardrails.pipeline import GuardrailContext, Stage, block, ok, warn
from schemas.execution import GuardrailResult

PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    # the NI *shape*, not HMRC's allocation rules: better a false positive than a leaked identifier
    "ni_number": re.compile(r"\b[A-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b", re.I),
    "sort_code": re.compile(r"\b\d{2}-\d{2}-\d{2}\b"),
    "uk_phone": re.compile(r"(?<![\d.])(?:\+44\s?7\d{3}|\(?07\d{3}\)?)\s?\d{3}\s?\d{3}\b"),
    "card_number": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "uk_postcode": re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b"),
    "date_of_birth": re.compile(r"\b(?:dob|date of birth|born)\b\W{0,3}\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", re.I),
}


def _luhn(digits: str) -> bool:
    nums = [int(d) for d in digits if d.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    for i, n in enumerate(reversed(nums)):
        if i % 2:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
    return total % 10 == 0


@dataclass
class PiiFinding:
    kind: str
    start: int
    end: int


def find_pii(text: str) -> list[PiiFinding]:
    out: list[PiiFinding] = []
    for kind, pattern in PII_PATTERNS.items():
        for m in pattern.finditer(text):
            if kind == "card_number" and not _luhn(m.group()):
                continue
            out.append(PiiFinding(kind, m.start(), m.end()))
    return out


def redact(text: str) -> tuple[str, list[str]]:
    findings = sorted(find_pii(text), key=lambda f: f.start, reverse=True)
    kinds: list[str] = []
    last_start = len(text) + 1
    for f in findings:
        if f.end > last_start:   # overlapping match already redacted
            continue
        text = text[:f.start] + f"[REDACTED:{f.kind}]" + text[f.end:]
        kinds.append(f.kind)
        last_start = f.start
    return text, sorted(set(kinds))


class PiiPolicy:
    """At the LLM boundary: PII is redacted before sending; if the caller forbids redaction, it blocks."""

    name = "pii_leakage"
    stages = frozenset({Stage.INPUT, Stage.OUTPUT_SCHEMA, Stage.MCP})

    def check(self, subject: object, ctx: GuardrailContext, stage: Stage) -> GuardrailResult:
        text = subject if isinstance(subject, str) else str(subject)
        kinds = sorted({f.kind for f in find_pii(text)})
        if not kinds:
            return ok(stage, self.name)
        if ctx.extra.get("redaction_applied"):
            return warn(stage, self.name, f"PII redacted before model call: {', '.join(kinds)}")
        if ctx.extra.get("allow_pii"):
            return warn(stage, self.name, f"PII present in a controlled store path: {', '.join(kinds)}")
        return block(stage, self.name, f"unredacted PII detected: {', '.join(kinds)}")
