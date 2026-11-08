"""Resilience tests (Phase 5a, scope #7). Offline, deterministic (no sleeps, no network).

Degrade gracefully, never cascade:
* a provider that returns 429 / 500 / times out is retried with backoff and, if it never
  recovers, surfaces a single typed `HttpClientError` (DR-002) instead of leaking httpx errors;
* a transient failure that then recovers returns the good response;
* an LLM that returns malformed JSON triggers the one-shot repair and, if that also fails, the
  section is marked insufficient rather than crashing the report (§17.2, HAL-001).

Partial-ingestion isolation (one bad record never aborts the batch) and per-section report
isolation are covered end-to-end by `tests/integration/test_sec_ingestion.py`
(`test_partial_failure_is_dead_lettered`) and `tests/integration/test_report_generation.py`
(`test_section_failure_isolated`); this file covers the offline provider-outage + LLM paths.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import httpx
import pytest

from app.citation.evidence import EvidenceItem
from app.providers.base import LLMMessage
from app.research.generator import generate_section
from app.schemas.sources import TextChunkSource
from app.utils.http import HardenedHttpClient, HttpClientError
from tests.fakes import FakeLLM

_HOSTS = frozenset({"data.sec.gov"})


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> HardenedHttpClient:
    ua = "AssayDesk/test (contact: dev@assay-desk.example)"
    inner = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": ua})
    return HardenedHttpClient(user_agent=ua, allowed_hosts=_HOSTS, rate_per_sec=1000, client=inner)


# --- provider outages (DR-002) --------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
def test_persistent_error_raises_typed_error(status: int) -> None:
    client = _client(lambda r: httpx.Response(status))
    with pytest.raises(HttpClientError, match="after retries"):
        client.get_bytes("https://data.sec.gov/x.json", sleep=lambda _s: None)


def test_rate_limited_then_recovers() -> None:
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] == 1 else httpx.Response(200, content=b"ok")

    client = _client(handler)
    assert client.get_bytes("https://data.sec.gov/x.json", sleep=lambda _s: None) == b"ok"
    assert calls["n"] == 2  # one retry after the 429


def test_timeout_is_retried_then_raises() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("timed out", request=request)

    client = _client(handler)
    with pytest.raises(HttpClientError):
        client.get_bytes("https://data.sec.gov/x.json", sleep=lambda _s: None)
    assert calls["n"] == 4  # initial try + 3 retries (max_retries default)


def test_timeout_then_success_recovers() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(200, content=b"recovered")

    client = _client(handler)
    assert client.get_bytes("https://data.sec.gov/x.json", sleep=lambda _s: None) == b"recovered"


# --- malformed LLM JSON → repair-then-fallback (§17.2) --------------------------------


def _risk_evidence() -> list[EvidenceItem]:
    text = "Supply chain constraints pose a risk to production and could reduce revenue."
    source = TextChunkSource(source_id="e1", tier=1, document_id="d1", text=text)
    return [EvidenceItem(source=source, text=text, retrieval_score=0.9)]


def test_malformed_json_section_marked_insufficient_not_crash() -> None:
    # `broken` → the model returns an invalid shape both times (initial + repair).
    result = generate_section(
        FakeLLM(broken={"risks"}),
        section="risks",
        payload={},
        evidence=_risk_evidence(),
    )
    assert result.insufficient is True
    assert result.items == []  # nothing fabricated from unparseable output
    assert result.failed is False  # the model answered; it is not a provider failure


class _RefusingLLM(FakeLLM):
    """Every call raises, as Gemini's 402 did once the prepaid credit ran out (2026-11-03)."""

    async def complete_json(
        self, messages: Sequence[LLMMessage], *, model: str, schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        raise HttpClientError("402 Payment Required")


def test_provider_error_on_both_attempts_marks_the_section_failed() -> None:
    """ERR-002, HAL-001: a refused call is a failure, not "insufficient evidence"."""
    result = generate_section(
        _RefusingLLM(), section="risks", payload={}, evidence=_risk_evidence()
    )
    assert result.insufficient is True and result.failed is True
    assert result.items == []


def test_valid_json_section_produces_verified_claim() -> None:
    result = generate_section(
        FakeLLM(),
        section="risks",
        payload={},
        evidence=_risk_evidence(),
    )
    assert result.insufficient is False
    accepted = [c for c in result.verified.claims if c.status in ("accepted", "softened")]
    assert accepted  # the repaired/clean path yields a citable claim
