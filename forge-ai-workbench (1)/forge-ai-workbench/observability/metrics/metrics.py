"""Process metrics (counters + histograms), mirrored to OpenTelemetry metrics when enabled."""
from __future__ import annotations

import threading
from collections import defaultdict


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[tuple[str, tuple], float] = defaultdict(float)
        self.histograms: dict[tuple[str, tuple], list[float]] = defaultdict(list)
        self._meter = None

    def enable_otel(self) -> None:
        from opentelemetry import metrics
        self._meter = metrics.get_meter("ai_workbench")

    def inc(self, name: str, amount: float = 1, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self.counters[key] += amount
        if self._meter is not None:
            self._meter.create_counter(name).add(amount, labels)

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self.histograms[key].append(value)
        if self._meter is not None:
            self._meter.create_histogram(name).record(value, labels)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "counters": {f"{n}{dict(l)}": v for (n, l), v in self.counters.items()},
                "histograms": {f"{n}{dict(l)}": {"count": len(v), "sum": sum(v)} for (n, l), v in self.histograms.items()},
            }


metrics = Metrics()
