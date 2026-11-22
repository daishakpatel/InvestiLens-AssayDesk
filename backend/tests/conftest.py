"""Keep the whole suite on mock providers and built-in defaults, whatever the developer's `.env`
or shell selects (DR-004): no `.env` file is read, so a real API key can never reach a test."""

import os

from app.config import Settings

Settings.model_config["env_file"] = None
os.environ["PROVIDER_MODE"] = "mock"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ["PRICE_PROVIDER"] = "mock"

# Spans go nowhere unless a test adds its own exporter: the app's default console exporter runs on
# a background thread (ADR-0068) that would print after pytest has closed its output.
from collections.abc import Sequence  # noqa: E402

from opentelemetry.sdk.trace import ReadableSpan  # noqa: E402
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult  # noqa: E402

from app.observability.tracing import configure_tracing  # noqa: E402


class _Discard(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return SpanExportResult.SUCCESS


configure_tracing(exporter=_Discard())
