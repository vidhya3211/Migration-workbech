"""Headroom compression policy (§11-12): what may be compressed, and how.

Critical evidence is never lossy-compressed. Protected content classes are PASS-THROUGH; bulk classes
(tool output, logs, search results, repetitive structured output) may be compressed *reversibly*: the
original is kept in the context store and a retrieval reference travels with the compressed text.
"""
from __future__ import annotations

from enum import StrEnum


class ContentClass(StrEnum):
    # protected - never compressed
    SYSTEM_PROMPT = "system_prompt"
    SOURCE_CODE_EVIDENCE = "source_code_evidence"
    SOURCE_LOCATOR = "source_locator"
    REGULATORY_EVIDENCE = "regulatory_evidence"
    FINANCIAL_INPUT = "financial_input"
    RECONCILIATION_VALUE = "reconciliation_value"
    CONDUCT_EVIDENCE = "conduct_evidence"
    GUARD_INPUT = "guard_input"
    PROVENANCE = "provenance"
    AUDIT = "audit"
    QUESTION = "question"
    # compressible (reversibly)
    TOOL_OUTPUT = "tool_output"
    MCP_OUTPUT = "mcp_output"
    LOG = "log"
    SEARCH_RESULT = "search_result"
    STRUCTURED_BULK = "structured_bulk"
    CONVERSATION = "conversation"


PROTECTED = frozenset({
    ContentClass.SYSTEM_PROMPT, ContentClass.SOURCE_CODE_EVIDENCE, ContentClass.SOURCE_LOCATOR,
    ContentClass.REGULATORY_EVIDENCE, ContentClass.FINANCIAL_INPUT, ContentClass.RECONCILIATION_VALUE,
    ContentClass.CONDUCT_EVIDENCE, ContentClass.GUARD_INPUT, ContentClass.PROVENANCE, ContentClass.AUDIT,
    ContentClass.QUESTION,
})


class CompressionMode(StrEnum):
    PASS_THROUGH = "pass_through"
    REVERSIBLE = "reversible"
    PASS_THROUGH_FALLBACK = "pass_through_fallback"   # compression failed or unavailable - explicit, logged
    DISABLED = "disabled"


POLICY_VERSION = "headroom-policy-1.0"


def mode_for(content_class: ContentClass, enabled: bool) -> CompressionMode:
    if not enabled:
        return CompressionMode.DISABLED
    if content_class in PROTECTED:
        return CompressionMode.PASS_THROUGH
    return CompressionMode.REVERSIBLE
