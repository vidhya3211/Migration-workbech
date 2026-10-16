"""Output guardrails: structured-output validity, citation integrity, unsupported claims."""
from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from guardrails.pipeline import GuardrailContext, Stage, block, ok, warn
from schemas.execution import GuardrailResult


@dataclass
class SchemaSubject:
    payload: object
    model: type[BaseModel]


class OutputSchemaPolicy:
    name = "output_schema"
    stages = frozenset({Stage.OUTPUT_SCHEMA})

    def check(self, subject: SchemaSubject, ctx: GuardrailContext, stage: Stage) -> GuardrailResult:
        try:
            if isinstance(subject.payload, subject.model):
                subject.model.model_validate(subject.payload.model_dump())
            else:
                subject.model.model_validate(subject.payload)
        except ValidationError as exc:
            return block(stage, self.name, f"invalid {subject.model.__name__}: {exc.error_count()} error(s): "
                                           f"{exc.errors()[0]['msg']}")
        return ok(stage, self.name, f"valid {subject.model.__name__}")


@dataclass
class CitedAnswer:
    answer: str
    cited_refs: list[str]
    allowed: dict[str, str]        # ref -> evidence text actually retrieved from the certified core
    refused: bool = False


_NUMBER = re.compile(r"(?<![A-Za-z0-9])[-+]?\d+(?:[.,]\d+)?%?")


def _numbers(text: str) -> set[str]:
    out = set()
    for n in _NUMBER.findall(text):
        n = n.replace(",", "").rstrip("%")
        try:
            out.add(str(float(n)))
        except ValueError:
            continue
    return out


class CitationPolicy:
    """Every citation must point at something actually retrieved; hallucinated citations are blocked."""

    name = "citation_integrity"
    stages = frozenset({Stage.OUTPUT_SCHEMA})

    def check(self, subject: CitedAnswer, ctx: GuardrailContext, stage: Stage) -> GuardrailResult:
        if subject.refused:
            return ok(stage, self.name, "refusal carries no citations")
        if not subject.cited_refs:
            return block(stage, self.name, "answer has no citations; certified answers must cite sources")
        unknown = [r for r in subject.cited_refs if r not in subject.allowed]
        if unknown:
            return block(stage, self.name, f"hallucinated citation(s): {', '.join(unknown)}")
        return ok(stage, self.name, f"{len(subject.cited_refs)} citation(s) verified")


class UnsupportedClaimPolicy:
    """Deterministic grounding check: every number stated in the answer must appear in the cited evidence."""

    name = "unsupported_claims"
    stages = frozenset({Stage.OUTPUT_SCHEMA})

    def check(self, subject: CitedAnswer, ctx: GuardrailContext, stage: Stage) -> GuardrailResult:
        if subject.refused:
            return ok(stage, self.name)
        evidence = " ".join(subject.allowed.get(r, "") for r in subject.cited_refs)
        unsupported = sorted(_numbers(subject.answer) - _numbers(evidence))
        if unsupported:
            return block(stage, self.name, f"figures not supported by cited evidence: {', '.join(unsupported)}")
        if len(subject.answer) > 4000:
            return warn(stage, self.name, "answer unusually long")
        return ok(stage, self.name)


@dataclass
class GroundedNarrative:
    """Model-written prose (briefs, stories, impact notes, hypotheses) and the facts it was given."""
    text: str
    facts: str
    allowed_values: frozenset[str] = frozenset()   # closed vocabulary fields (e.g. an action) must be one of these
    value: str | None = None


class GroundedNarrativePolicy:
    """Every figure in model prose must appear in the facts supplied to it; closed-vocabulary values must be
    from the allowed set. Blocks rather than repairs - the agent then keeps its deterministic text."""

    name = "grounded_narrative"
    stages = frozenset({Stage.OUTPUT_SCHEMA})

    def check(self, subject: GroundedNarrative, ctx: GuardrailContext, stage: Stage) -> GuardrailResult:
        if subject.allowed_values and subject.value not in subject.allowed_values:
            return block(stage, self.name, f"value {subject.value!r} is not one of {sorted(subject.allowed_values)}")
        unsupported = sorted(_numbers(subject.text) - _numbers(subject.facts))
        if unsupported:
            return block(stage, self.name, f"figures not present in the facts: {', '.join(unsupported[:5])}")
        if not subject.text.strip():
            return block(stage, self.name, "empty narrative")
        return ok(stage, self.name, "narrative grounded in the supplied facts")
