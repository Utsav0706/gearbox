"""Tracing: every API request as an OpenTelemetry trace, exported to Langfuse.

One trace per request: a root span "gearbox.request" with the routing decision, cache,
policy, fallback path, tokens and cost as attributes, and one "generation" child span
per model call (several when a fallback ran). Dyno reads these traces later.

OpenTelemetry rather than Langfuse's own SDK: it is the vendor-neutral standard (Scout
uses it too), and Langfuse ingests OTLP directly, so a different backend is one URL.

Off unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set: with no provider, every
span is a free no-op. No prompt or answer text is sent, as in the request log: the
trace holds metadata only.
"""

from __future__ import annotations

import base64
import logging

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from gearbox.config import Secrets

log = logging.getLogger(__name__)
SCOPE = "gearbox"


class OnlyGearboxSpans(SpanProcessor):
    """Export only spans Gearbox creates. Libraries can trace themselves too, and on
    Scout their copies of each model call doubled the cost Langfuse showed."""

    def __init__(self, delegate: SpanProcessor) -> None:
        self._delegate = delegate

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        self._delegate.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        if span.instrumentation_scope is not None and span.instrumentation_scope.name == SCOPE:
            self._delegate.on_end(span)

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._delegate.force_flush(timeout_millis)


def make_provider(exporter: SpanExporter, processor_cls=BatchSpanProcessor) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": "gearbox"}))
    provider.add_span_processor(OnlyGearboxSpans(processor_cls(exporter)))
    return provider


def langfuse_settings(secrets: Secrets) -> tuple[str, dict[str, str]] | None:
    """(OTLP endpoint, headers) for Langfuse, or None when its keys aren't set."""
    if not (secrets.langfuse_public_key and secrets.langfuse_secret_key):
        return None
    pair = (
        f"{secrets.langfuse_public_key.get_secret_value()}:"
        f"{secrets.langfuse_secret_key.get_secret_value()}"
    )
    endpoint = f"{secrets.langfuse_host.rstrip('/')}/api/public/otel/v1/traces"
    headers = {
        "Authorization": "Basic " + base64.b64encode(pair.encode()).decode(),
        # Without this header Langfuse can take up to 10 minutes to show OTel data.
        "x-langfuse-ingestion-version": "4",
    }
    return endpoint, headers


def setup_tracing(secrets: Secrets) -> bool:
    """Install the Langfuse exporter once per process. Returns whether tracing is on."""
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return True  # already set up (or a test installed an in-memory one)
    settings = langfuse_settings(secrets)
    if settings is None:
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    endpoint, headers = settings
    trace.set_tracer_provider(make_provider(OTLPSpanExporter(endpoint=endpoint, headers=headers)))
    log.info("tracing to Langfuse", extra={"host": secrets.langfuse_host})
    return True


def tracer() -> trace.Tracer:
    return trace.get_tracer(SCOPE)
