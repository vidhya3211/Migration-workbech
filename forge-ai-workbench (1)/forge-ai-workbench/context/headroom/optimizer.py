"""Headroom context optimiser (§11-13): sits between tool/MCP results and the LLM context boundary.

    Tool / MCP result -> [detect class] -> protected? PASS-THROUGH
                                        -> bulk?      store original (CCR) -> headroom.compress -> + retrieval ref
                      -> token accounting (before/after/ratio/mode/policy) -> LLM

Modes: ``library`` (in-process ``headroom.compress``), ``proxy`` (a self-hosted Headroom proxy inside the
private network does the compression; the LLM provider's base URL points at it and metrics come back on the
response), ``disabled``. A compression failure is never silent: the block passes through unchanged with
mode ``pass_through_fallback`` and the event is logged. Token cost is always reported, never hidden.
"""
from __future__ import annotations

from dataclasses import dataclass

from config.settings import HeadroomSettings
from context.headroom.policy import POLICY_VERSION, PROTECTED, CompressionMode, ContentClass, mode_for
from context.headroom.store import ContextStore
from observability.cost.ledger import count_tokens
from observability.logging.logger import Event, log_event
from observability.tracing.tracer import SpanKind, get_tracer
from schemas.execution import HeadroomMetrics


@dataclass
class ContextBlock:
    block_id: str
    role: str                      # system | user | assistant | tool
    content: str
    content_class: ContentClass
    classification: str = "internal"


@dataclass
class OptimizedContext:
    messages: list[dict]
    metrics: list[HeadroomMetrics]

    @property
    def tokens_before(self) -> int:
        return sum(m.tokens_before for m in self.metrics)

    @property
    def tokens_after(self) -> int:
        return sum(m.tokens_after for m in self.metrics)

    @property
    def compression_ratio(self) -> float:
        b = self.tokens_before
        return round((b - self.tokens_after) / b, 4) if b else 0.0


class HeadroomOptimizer:
    def __init__(self, settings: HeadroomSettings, store: ContextStore, environment: str) -> None:
        self.settings = settings
        self.store = store
        self.environment = environment
        self._compress = None
        if settings.mode == "library":
            try:
                from headroom import compress
                self._compress = compress
            except Exception as exc:   # library missing: explicit fallback, visible in telemetry
                log_event(Event.HEADROOM_OPTIMIZED, f"headroom library unavailable: {exc}", mode="unavailable")

    @property
    def enabled(self) -> bool:
        if self.settings.mode == "disabled":
            return False
        if self.environment == "production" and not self.settings.production_optimization_enabled:
            return False   # Phase 7 gate: enabled in production only after compression-vs-quality evals pass
        return True

    def optimize(self, run_id: str, blocks: list[ContextBlock], model: str = "gpt-4o") -> OptimizedContext:
        messages: list[dict] = []
        metrics: list[HeadroomMetrics] = []
        with get_tracer().span(SpanKind.HEADROOM, "optimize", blocks=len(blocks)) as span:
            for b in blocks:
                before = count_tokens(b.content, model)
                mode = mode_for(b.content_class, self.enabled)
                content, after, ref, transforms = b.content, before, None, []
                if mode is CompressionMode.REVERSIBLE and self.settings.mode == "library":
                    if before < self.settings.min_tokens_to_compress:
                        mode = CompressionMode.PASS_THROUGH
                    elif self._compress is None:
                        mode = CompressionMode.PASS_THROUGH_FALLBACK
                    else:
                        try:
                            ref = self.store.put(run_id, b.content, b.classification)
                            res = self._compress([{"role": "user", "content": b.content}], model=model,
                                                 compress_user_messages=True, protect_recent=0,
                                                 kompress_model=self.settings.kompress_model,
                                                 target_ratio=self.settings.target_ratio)
                            compressed = res.messages[0]["content"]
                            if not isinstance(compressed, str):
                                compressed = str(compressed)
                            content = f"{compressed}\n[aiwb-context-ref {ref}: full original retrievable]"
                            after = count_tokens(content, model)
                            transforms = list(res.transforms_applied)
                            if after >= before:      # no gain: keep the original, still reversible-safe
                                content, after, transforms = b.content, before, transforms + ["no_gain_reverted"]
                        except Exception as exc:
                            mode, content, after, ref = CompressionMode.PASS_THROUGH_FALLBACK, b.content, before, None
                            log_event(Event.HEADROOM_OPTIMIZED, f"compression failed, passing through: {exc}",
                                      block_id=b.block_id, mode=mode.value)
                elif mode is CompressionMode.REVERSIBLE and self.settings.mode == "proxy":
                    ref = self.store.put(run_id, b.content, b.classification)   # proxy compresses in-flight
                if b.content_class in PROTECTED and content != b.content:
                    raise RuntimeError(f"protected content {b.block_id} was altered - refusing to continue")
                ratio = round((before - after) / before, 4) if before else 0.0
                metrics.append(HeadroomMetrics(
                    block_id=b.block_id, content_type=b.content_class.value, compression_mode=mode.value,
                    compression_policy=POLICY_VERSION, tokens_before=before, tokens_after=after,
                    compression_ratio=ratio, original_reference=ref, retrieval_available=ref is not None,
                    transforms=transforms))
                messages.append({"role": b.role, "content": content})
            span.set(tokens_before=sum(m.tokens_before for m in metrics),
                     tokens_after=sum(m.tokens_after for m in metrics))
        log_event(Event.HEADROOM_OPTIMIZED, blocks=len(blocks), tokens_before=sum(m.tokens_before for m in metrics),
                  tokens_after=sum(m.tokens_after for m in metrics))
        return OptimizedContext(messages, metrics)

    def retrieve(self, ref: str) -> str:
        return self.store.get(ref)
