"""HardenedHttpClient tests: UA header, rate spacing, retries, SSRF (DR-002, LGL-002, SEC-015).

Offline — uses httpx.MockTransport, no real network.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from app.utils.http import CircuitBreaker, HardenedHttpClient, HttpClientError, RateLimiter

_HOSTS = frozenset({"data.sec.gov"})


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    ua: str = "AssayDesk/test (contact: dev@assay-desk.example)",
) -> HardenedHttpClient:
    transport = httpx.MockTransport(handler)
    inner = httpx.Client(transport=transport, headers={"User-Agent": ua})
    return HardenedHttpClient(user_agent=ua, allowed_hosts=_HOSTS, rate_per_sec=1000, client=inner)


def test_rate_limiter_spaces_calls() -> None:
    """LGL-002: the limiter enforces a minimum interval between calls."""
    clock = {"t": 0.0}
    slept: list[float] = []
    limiter = RateLimiter(rate_per_sec=5.0)  # 0.2s min interval
    limiter.acquire(sleep=lambda s: slept.append(s), now=lambda: clock["t"])  # first: no wait
    limiter.acquire(sleep=lambda s: slept.append(s), now=lambda: clock["t"])  # immediate: waits
    assert slept == [pytest.approx(0.2, abs=1e-6)]  # only the second call waits


def test_rate_limiter_gives_concurrent_callers_separate_slots() -> None:
    """LGL-002, NFR-003: callers arriving together (parallel report sections) still get spaced."""
    slept: list[float] = []
    limiter = RateLimiter(rate_per_sec=5.0)
    for _ in range(3):
        limiter.acquire(sleep=slept.append, now=lambda: 0.0)  # the clock has not moved
    assert slept == [pytest.approx(0.2, abs=1e-6), pytest.approx(0.4, abs=1e-6)]


def test_sends_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, content=b"ok")

    client = _client(handler)
    body = client.get_bytes("https://data.sec.gov/x.json", sleep=lambda _s: None)
    assert body == b"ok"
    assert "AssayDesk" in seen["ua"] and "contact:" in seen["ua"]


def test_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=b"recovered")

    client = _client(handler)
    body = client.get_bytes("https://data.sec.gov/x.json", sleep=lambda _s: None)
    assert body == b"recovered"
    assert calls["n"] == 2  # one retry


def test_client_error_is_not_retried() -> None:
    """A 4xx other than 429 (a bad API key, a missing page) fails at once, not after retries."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401)

    with pytest.raises(HttpClientError, match="401"):
        _client(handler).get_bytes("https://data.sec.gov/x.json", sleep=lambda _s: None)
    assert calls["n"] == 1


def test_ssrf_guard_blocks_disallowed_host() -> None:
    client = _client(lambda r: httpx.Response(200))
    with pytest.raises(HttpClientError, match="SSRF"):
        client.get_bytes("https://evil.example/x", sleep=lambda _s: None)


def test_circuit_breaker_opens_after_threshold() -> None:
    breaker = CircuitBreaker(threshold=2, reset_seconds=999)
    breaker.record_failure(now=lambda: 0.0)
    breaker.record_failure(now=lambda: 0.0)
    with pytest.raises(HttpClientError, match="open"):
        breaker.before_request(now=lambda: 1.0)
