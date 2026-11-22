"""OpenTelemetry tracing (OBS-001, ADR-0021).

Spans cover API → retrieval → LLM → citation validator, carrying `company`, `intent`, and
`prompt_version` as span attributes so one `request_id` lets you reconstruct the full life of a
question. Zero infra required: the default exporter prints spans as JSON to stdout, which is
enough to debug locally and in CI. Setting `otel_exporter_otlp_endpoint` switches to a real OTLP
collector (Jaeger/Tempo/Honeycomb/...) with no code change — see ADR-0021.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Span

from app.config import get_settings

_PROVIDER: TracerProvider | None = None


def configure_tracing(*, exporter: SpanExporter | None = None) -> TracerProvider:
    """Install (once) or add an exporter to the process-wide `TracerProvider`.

    OpenTelemetry only allows `set_tracer_provider` to succeed once per process, so the provider
    itself is created on the first call and reused after; each call still attaches its `exporter`
    as a new processor on that one provider. This lets tests inject an `InMemorySpanExporter` per
    test (each sees only the spans created after it's added) without any collector running, while
    a real app/script still gets a single provider configured once at startup.
    """
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = TracerProvider(resource=Resource.create({SERVICE_NAME: "assay-desk-api"}))
        trace.set_tracer_provider(_PROVIDER)
    # An injected exporter (tests) sees each span as it ends. The app's own exporter runs on a
    # background thread: exporting inline put a JSON dump of every span on the request path, which
    # the 2026-11-22 load test showed in the p95 (ADR-0068).
    _PROVIDER.add_span_processor(
        SimpleSpanProcessor(exporter) if exporter else BatchSpanProcessor(_default_exporter())
    )
    return _PROVIDER


def _default_exporter() -> SpanExporter:
    settings = get_settings()
    if settings.otel_exporter_otlp_endpoint:
        try:
            # Optional extra (ADR-0021): only needed for a real deployment's OTLP collector.
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
                OTLPSpanExporter,
            )

            return OTLPSpanExporter(  # type: ignore[no-any-return]
                endpoint=settings.otel_exporter_otlp_endpoint
            )
        except ImportError:
            pass
    return ConsoleSpanExporter()


def get_tracer(name: str) -> trace.Tracer:
    if _PROVIDER is None:
        configure_tracing()
    return trace.get_tracer(name)


@contextmanager
def span(
    name: str, *, tracer_name: str = __name__, attributes: dict[str, Any] | None = None
) -> Iterator[Span]:
    """Start a span named `name`, setting any known `attributes` up front (OBS-001)."""
    tracer = get_tracer(tracer_name)
    with tracer.start_as_current_span(name, attributes=attributes or {}) as current:
        yield current


__all__ = ["ReadableSpan", "configure_tracing", "get_tracer", "span"]
