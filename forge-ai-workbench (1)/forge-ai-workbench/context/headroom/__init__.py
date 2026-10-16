"""Headroom integration: context optimisation with protected pass-through and reversible compression."""
from context.headroom.optimizer import ContextBlock, HeadroomOptimizer, OptimizedContext
from context.headroom.policy import ContentClass, CompressionMode

__all__ = ["CompressionMode", "ContentClass", "ContextBlock", "HeadroomOptimizer", "OptimizedContext"]
