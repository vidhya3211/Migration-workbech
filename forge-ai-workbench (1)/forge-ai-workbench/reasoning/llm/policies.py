"""LLM usage policies: which purposes exist, what content classes they may see, and their output caps.

LLMs interpret, explain and propose. They never certify, approve, calculate authoritatively, reconcile or
write to the knowledge core - those purposes do not exist here and the control plane refuses them.
"""
from __future__ import annotations

from dataclasses import dataclass

from context.headroom.policy import ContentClass as C


@dataclass(frozen=True)
class PurposePolicy:
    purpose: str
    prompt_id: str
    allowed_content: frozenset[C]
    max_output_tokens: int
    allow_pii: bool = False


POLICIES: dict[str, PurposePolicy] = {p.purpose: p for p in [
    PurposePolicy("interpret_unparsed_statement", "interpret_unparsed_statement",
                  frozenset({C.SYSTEM_PROMPT, C.SOURCE_CODE_EVIDENCE, C.SOURCE_LOCATOR, C.TOOL_OUTPUT}), 800),
    PurposePolicy("extract_assertions", "extract_assertions",
                  frozenset({C.SYSTEM_PROMPT, C.REGULATORY_EVIDENCE, C.SOURCE_LOCATOR}), 800),
    PurposePolicy("canonicalise_name", "canonicalise_name", frozenset({C.SYSTEM_PROMPT, C.STRUCTURED_BULK}), 300),
    PurposePolicy("explain_difference", "explain_difference",
                  frozenset({C.SYSTEM_PROMPT, C.SOURCE_CODE_EVIDENCE, C.GUARD_INPUT, C.STRUCTURED_BULK}), 500),
    PurposePolicy("semantic_mapping_tiebreak", "semantic_mapping_tiebreak",
                  frozenset({C.SYSTEM_PROMPT, C.STRUCTURED_BULK}), 400),
    PurposePolicy("transformation_plan", "transformation_plan", frozenset({C.SYSTEM_PROMPT, C.STRUCTURED_BULK}), 600),
    PurposePolicy("root_cause_hypotheses", "root_cause_hypotheses",
                  frozenset({C.SYSTEM_PROMPT, C.STRUCTURED_BULK, C.LOG}), 500),
    PurposePolicy("compose_cited_answer", "compose_cited_answer",
                  frozenset({C.SYSTEM_PROMPT, C.QUESTION, C.SEARCH_RESULT, C.REGULATORY_EVIDENCE}), 700),
    PurposePolicy("adjudication_brief", "adjudication_brief",
                  frozenset({C.SYSTEM_PROMPT, C.GUARD_INPUT, C.SOURCE_CODE_EVIDENCE, C.STRUCTURED_BULK}), 900),
    PurposePolicy("user_story", "user_story", frozenset({C.SYSTEM_PROMPT, C.GUARD_INPUT, C.STRUCTURED_BULK}), 900),
    PurposePolicy("cleansing_proposal", "cleansing_proposal", frozenset({C.SYSTEM_PROMPT, C.STRUCTURED_BULK}), 500),
    PurposePolicy("conduct_impact_note", "conduct_impact_note",
                  frozenset({C.SYSTEM_PROMPT, C.STRUCTURED_BULK, C.GUARD_INPUT}), 700),
    PurposePolicy("break_hypothesis", "break_hypothesis", frozenset({C.SYSTEM_PROMPT, C.STRUCTURED_BULK}), 500),
]}


def policy_for(purpose: str) -> PurposePolicy:
    try:
        return POLICIES[purpose]
    except KeyError:
        raise ValueError(f"no LLM policy for purpose {purpose!r}") from None
