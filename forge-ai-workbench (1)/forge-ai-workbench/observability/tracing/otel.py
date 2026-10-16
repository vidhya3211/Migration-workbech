"""OpenTelemetry bridge. Exports to Azure Monitor / Application Insights when the exporter package and
``APPLICATIONINSIGHTS_CONNECTION_STRING`` (from Key Vault) are present; otherwise to an OTLP endpoint if
``OTEL_EXPORTER_OTLP_ENDPOINT`` is set; otherwise spans stay in-process (console sink only)."""
from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def build_otel_tracer(service_name: str) -> trace.Tracer:
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if conn:
        from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter  # optional dependency
        provider.add_span_processor(BatchSpanProcessor(AzureMonitorTraceExporter(connection_string=conn)))
    elif os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # optional dependency
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return trace.get_tracer("ai_workbench")
