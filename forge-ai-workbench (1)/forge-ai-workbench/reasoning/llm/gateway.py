"""LLM gateway - the only way an agent reaches a model (§6, §13, §15).

    agent intent -> control plane (tier + purpose) -> input guardrails (injection, PII redaction)
                 -> Headroom (protected pass-through, reversible compression) -> token budget guard
                 -> run ledger reservation -> provider call (retry + backoff) -> cost accounting
                 -> structured-output validation -> output guardrails -> LLMCallRecord (+ trace, log, DB)

Output is untrusted: the caller still sends anything derived from it through the deterministic guard.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from pydantic import BaseModel

from config.settings import Settings
from context.headroom.optimizer import ContextBlock, HeadroomOptimizer
from context.headroom.policy import ContentClass
from guardrails.output.policies import SchemaSubject
from guardrails.pii.detector import redact
from guardrails.pipeline import GuardrailContext, GuardrailPipeline, Stage
from guardrails.policy.policies import LLMIntent
from observability.cost.ledger import RunLedger, count_tokens, estimate_cost
from observability.logging.logger import Event, log_event
from observability.tracing.tracer import SpanKind, get_tracer
from reasoning.llm.factory import resolve_model
from reasoning.llm.policies import policy_for
from reasoning.llm.prompts import get_prompt
from reasoning.llm.provider import LLMError, LLMProvider, LLMRequest, LLMUnavailable
from reasoning.llm.structured_output import StructuredOutputError, parse_structured
from schemas.common import new_id
from schemas.execution import HeadroomMetrics, LLMCallRecord

T = TypeVar("T", bound=BaseModel)
Recorder = Callable[[LLMCallRecord, list[HeadroomMetrics]], None]


@dataclass
class Evidence:
    """A piece of context for the model, tagged with its content class (drives Headroom policy)."""
    key: str
    text: str
    content_class: ContentClass
    classification: str = "internal"


@dataclass
class LLMOutcome:
    parsed: BaseModel | None
    record: LLMCallRecord
    headroom: list[HeadroomMetrics] = field(default_factory=list)
    error: str | None = None


class LLMGateway:
    def __init__(self, settings: Settings, provider: LLMProvider, optimizer: HeadroomOptimizer,
                 guardrails: GuardrailPipeline, recorder: Recorder | None = None) -> None:
        self.settings = settings
        self.provider = provider
        self.optimizer = optimizer
        self.guardrails = guardrails
        self.recorder = recorder

    @property
    def available(self) -> bool:
        return self.provider.name != "offline"

    def call(self, *, agent: str, run_id: str, purpose: str, variables: dict[str, str], evidence: list[Evidence],
             output_model: type[T], ledger: RunLedger | None = None, model_name: str | None = None) -> LLMOutcome:
        pol = policy_for(purpose)
        prompt = get_prompt(pol.prompt_id)
        model = resolve_model(self.settings, model_name)
        ctx = GuardrailContext(run_id=run_id, agent=agent, extra={"environment": self.settings.environment})
        call_id = new_id("LLM")
        record = LLMCallRecord(call_id=call_id, run_id=run_id, agent=agent, purpose=purpose,
                               provider=self.provider.name, model=model.name, prompt_id=prompt.id,
                               prompt_version=prompt.version, input_tokens=0, compressed_input_tokens=0,
                               output_tokens=0, estimated_cost="0", latency_ms=0, compression_ratio=0.0,
                               status="refused")
        log_event(Event.LLM_STARTED, purpose=purpose, model=model.name, prompt_version=prompt.version)
        with get_tracer().span(SpanKind.LLM, purpose, model=model.name, provider=self.provider.name,
                               prompt_version=prompt.version) as span:
            record.trace_id, record.span_id = span.span.trace_id, span.span.span_id
            # 1. content-class policy + untrusted-input guardrails
            system_text = prompt.system.replace("{business_domain}",
                                                variables.get("business_domain") or "business")
            blocks: list[ContextBlock] = [ContextBlock("system", "system", system_text, ContentClass.SYSTEM_PROMPT)]
            redacted_kinds: set[str] = set()
            for ev in evidence:
                if ev.content_class not in pol.allowed_content:
                    raise PermissionError(f"{purpose} may not receive {ev.content_class.value} content")
                self.guardrails.run(Stage.INPUT, ev.text, ctx, only={"prompt_injection"})
                text = ev.text
                if not pol.allow_pii:
                    text, kinds = redact(text)
                    redacted_kinds |= set(kinds)
                blocks.append(ContextBlock(ev.key, "user", f'<evidence id="{ev.key}">\n{text}\n</evidence>',
                                           ev.content_class, ev.classification))
            vars_redacted = {k: redact(v)[0] if not pol.allow_pii else v for k, v in variables.items()}
            instruction = prompt.render(**vars_redacted, evidence="(see the evidence blocks above)",
                                        sources="(see the evidence blocks above)")
            blocks.append(ContextBlock("instruction", "user", instruction, ContentClass.QUESTION))
            self.guardrails.run(Stage.INPUT, "\n".join(b.content for b in blocks[1:]),
                                GuardrailContext(run_id, agent, {"redaction_applied": True}), only={"pii_leakage"})
            # 2. Headroom: each block keeps its own class - protected blocks pass through, bulk compresses reversibly
            optimized = self.optimizer.optimize(run_id, blocks, model.name)
            tokens_before, tokens_after = optimized.tokens_before, optimized.tokens_after
            record.input_tokens, record.compressed_input_tokens = tokens_before, tokens_after
            record.compression_ratio = optimized.compression_ratio
            # 3. tier / purpose + token budget guardrails, then reserve the run budget
            intent = LLMIntent(purpose=purpose, prompt_chars=sum(len(m["content"]) for m in optimized.messages),
                               projected_tokens=tokens_after, max_input_tokens=model.max_input_tokens)
            self.guardrails.run(Stage.INPUT, intent, ctx, only={"autonomy_tier"})
            self.guardrails.run(Stage.TOKEN_BUDGET, intent, ctx)
            if ledger is not None:
                ledger.reserve_llm_call(tokens_after)
            request = LLMRequest(purpose=purpose, prompt_id=prompt.id, prompt_version=prompt.version,
                                 messages=optimized.messages, max_output_tokens=pol.max_output_tokens,
                                 metadata={"run_id": run_id, "agent": agent, "call_id": call_id})
            # 4. provider call with bounded retry/backoff
            started = time.perf_counter()
            try:
                response = self._with_retry(request, model)
            except LLMUnavailable as exc:
                record.status, record.error = "unavailable", str(exc)
                span.set(status="unavailable")
                self._record(record, optimized.metrics)
                return LLMOutcome(None, record, optimized.metrics, str(exc))
            except LLMError as exc:
                record.status, record.error = "error", str(exc)[:500]
                record.latency_ms = (time.perf_counter() - started) * 1000
                span.fail(str(exc))
                self._record(record, optimized.metrics)
                log_event(Event.LLM_FAILED, str(exc)[:300], purpose=purpose)
                return LLMOutcome(None, record, optimized.metrics, str(exc))
            record.latency_ms = response.latency_ms
            record.output_tokens = response.usage.output_tokens or count_tokens(response.content)
            record.cached_tokens = response.usage.cached_tokens
            if response.usage.input_tokens:
                record.compressed_input_tokens = response.usage.input_tokens
            cost = estimate_cost(self.settings.llm.models[model.name], record.compressed_input_tokens,
                                 record.output_tokens, record.cached_tokens)
            record.estimated_cost = str(cost)
            if ledger is not None:
                ledger.record_llm(agent, record.input_tokens, record.compressed_input_tokens, record.output_tokens,
                                  record.cached_tokens, cost)
            # 5. structured output + output guardrails
            try:
                parsed = parse_structured(response.content, output_model)
                self.guardrails.run(Stage.OUTPUT_SCHEMA, SchemaSubject(parsed, output_model), ctx,
                                    only={"output_schema"})
                record.status = "ok"
            except StructuredOutputError as exc:
                record.status, record.error = "schema_invalid", str(exc)
                parsed = None
            span.set(input_tokens=record.input_tokens, compressed_input_tokens=record.compressed_input_tokens,
                     output_tokens=record.output_tokens, cost=record.estimated_cost, status=record.status,
                     pii_redacted=",".join(sorted(redacted_kinds)))
        self._record(record, optimized.metrics)
        log_event(Event.LLM_COMPLETED, purpose=purpose, status=record.status, input_tokens=record.input_tokens,
                  compressed_input_tokens=record.compressed_input_tokens, output_tokens=record.output_tokens,
                  cost=record.estimated_cost)
        return LLMOutcome(parsed, record, optimized.metrics, record.error)

    def _with_retry(self, request: LLMRequest, model):
        attempts = self.settings.llm.max_retries + 1
        delay = 0.5
        for attempt in range(attempts):
            try:
                return self.provider.complete(request, model)
            except LLMError as exc:
                if not exc.retryable or attempt == attempts - 1:
                    raise
                time.sleep(delay)
                delay *= 2
        raise LLMError("unreachable")

    def _record(self, record: LLMCallRecord, metrics: list[HeadroomMetrics]) -> None:
        if self.recorder is not None:
            self.recorder(record, metrics)
