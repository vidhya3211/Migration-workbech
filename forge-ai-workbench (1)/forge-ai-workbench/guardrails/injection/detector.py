"""Prompt / tool injection detection.

Applied to anything untrusted that could reach a model or steer an agent: uploaded documents and code
comments, MCP output, tool results, search results, user questions. Deterministic and explainable:
each hit names the pattern that fired.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from guardrails.pipeline import GuardrailContext, Stage, block, ok, warn
from schemas.common import value_ref
from schemas.execution import GuardrailResult

_I = re.IGNORECASE
PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    ("override_instructions", re.compile(r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|system|your)\b.{0,30}\b(instructions?|rules?|prompts?|guidelines?|directions?)", _I), 0.8),
    ("role_hijack", re.compile(r"\b(you are now|act as|pretend to be|from now on you)\b", _I), 0.45),
    ("system_prompt_probe", re.compile(r"\b(reveal|print|show|repeat|output)\b.{0,30}\b(system prompt|hidden instructions|your instructions)", _I), 0.7),
    ("approval_steering", re.compile(r"\b(auto-?approve|mark (this|it|all) as (certified|approved)|set status to (certified|approved)|skip (the )?(review|guard|approval))\b", _I), 0.85),
    ("bulk_approval", re.compile(r"\b(approve|certify|sign off|accept)\b.{0,20}\b(every|all|each|everything)\b", _I), 0.6),
    ("fake_role_header", re.compile(r"(^|\n)\s*(system|assistant|developer)\s*:", _I), 0.5),
    ("tool_call_injection", re.compile(r"(<\s*/?\s*(tool_call|function_call|system|assistant)\s*>|\"tool_calls\"\s*:|\bcall the tool\b)", _I), 0.6),
    ("exfiltration", re.compile(r"\b(send|post|upload|exfiltrate|email)\b.{0,40}\b(https?://|data|credentials|secrets?|api key|password)", _I), 0.55),
    ("credential_request", re.compile(r"\b(api[_ -]?key|password|secret|token)\b.{0,20}\b(is|=|:)", _I), 0.3),
    ("delimiter_escape", re.compile(r"(```\s*system|\[/?INST\]|<\|im_start\|>|<\|endoftext\|>)", _I), 0.6),
]
_INVISIBLE = {"Cf"}


@dataclass
class InjectionScan:
    score: float
    hits: list[str]


def scan(text: str) -> InjectionScan:
    hits: list[str] = []
    score = 0.0
    for name, pattern, weight in PATTERNS:
        if pattern.search(text):
            hits.append(name)
            score = score + weight * (1 - score)   # noisy-or aggregation
    invisible = sum(1 for ch in text if unicodedata.category(ch) in _INVISIBLE and ch not in "‍﻿")
    if invisible >= 3:
        hits.append("invisible_unicode")
        score = score + 0.4 * (1 - score)
    return InjectionScan(round(score, 3), hits)


class InjectionPolicy:
    """Blocks at or above the threshold; warns on weaker signals so they are visible in the console."""

    name = "prompt_injection"
    stages = frozenset({Stage.INPUT, Stage.MCP, Stage.TOOL})

    def __init__(self, threshold: float = 0.6) -> None:
        self.threshold = threshold

    def check(self, subject: object, ctx: GuardrailContext, stage: Stage) -> GuardrailResult:
        text = subject if isinstance(subject, str) else str(subject)
        res = scan(text)
        ref = value_ref(text)
        if res.score >= self.threshold:
            return block(stage, self.name, f"injection score {res.score} ({', '.join(res.hits)})", ref)
        if res.hits:
            return warn(stage, self.name, f"weak injection signals {res.score} ({', '.join(res.hits)})", ref)
        return ok(stage, self.name, subject_ref=ref)
