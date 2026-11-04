"""Hardened HTTP client for external providers (DR-002, LGL-002, SEC-015).

Provides, in one place: a descriptive User-Agent, a token-bucket rate limiter, retry with
exponential backoff + jitter on transient failures, a circuit breaker, and an SSRF host
allowlist. Used by the live SEC client; other live providers can reuse it.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class HttpClientError(RuntimeError):
    """Non-retryable client error (bad host, circuit open, exhausted retries)."""


class RateLimiter:
    """Token-bucket-ish limiter: enforces a minimum interval between calls (ING-006)."""

    def __init__(self, rate_per_sec: float) -> None:
        self._min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._last: float | None = None  # None = no call yet (first call never waits)
        self._lock = threading.Lock()

    def acquire(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        with self._lock:  # reserve a slot, and sleep outside so the next caller can take its own
            current = now()
            start = current if self._last is None else max(current, self._last + self._min_interval)
            self._last = start
        if start > current:
            sleep(start - current)


@dataclass
class CircuitBreaker:
    """Opens after `threshold` consecutive failures; stays open for `reset_seconds`."""

    threshold: int = 5
    reset_seconds: float = 30.0
    _failures: int = field(default=0)
    _opened_at: float | None = field(default=None)

    def before_request(self, *, now: Callable[[], float] = time.monotonic) -> None:
        if self._opened_at is not None:
            if now() - self._opened_at < self.reset_seconds:
                raise HttpClientError("circuit breaker open")
            self._opened_at = None  # half-open: allow a trial request

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = now()


class HardenedHttpClient:
    """Synchronous HTTP GET with UA, rate limit, retries, circuit breaker, SSRF allowlist."""

    def __init__(
        self,
        *,
        user_agent: str,
        allowed_hosts: frozenset[str],
        rate_per_sec: float,
        timeout: float = 20.0,
        max_retries: int = 3,
        default_headers: dict[str, str] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._allowed_hosts = allowed_hosts
        self._limiter = RateLimiter(rate_per_sec)
        self._breaker = CircuitBreaker()
        self._max_retries = max_retries
        # Auth headers (e.g. a provider token) are kept out of the URL so they never hit logs.
        headers = {"User-Agent": user_agent, **(default_headers or {})}
        self._client = client or httpx.Client(
            headers=headers,
            timeout=timeout,
            follow_redirects=False,  # SEC-015: don't follow redirects to arbitrary hosts
        )

    def _check_host(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        if host not in self._allowed_hosts:
            raise HttpClientError(f"host not allowed (SSRF guard): {host!r}")

    def get_bytes(self, url: str, *, sleep: Callable[[float], None] = time.sleep) -> bytes:
        """GET `url`, returning the raw body (retries + circuit breaker + SSRF guard)."""
        return self._request("GET", url, sleep=sleep)

    def post_bytes(
        self, url: str, *, json: Any, sleep: Callable[[float], None] = time.sleep
    ) -> bytes:
        """POST a JSON body to `url`, returning the raw response body.

        Shares the GET path's hardening (rate limit, retry+backoff, breaker, SSRF allowlist); used
        by providers whose API is request/response rather than fetch (e.g. Voyage embeddings).
        """
        return self._request("POST", url, json=json, sleep=sleep)

    def _request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> bytes:
        self._check_host(url)  # SEC-015
        self._breaker.before_request()
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self._limiter.acquire(sleep=sleep)
            try:
                response = self._client.request(method, url, json=json)
                if response.status_code in RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"retryable status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                self._breaker.record_success()
                return response.content
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                status = (
                    exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                )
                if status is not None and status not in RETRYABLE_STATUS:
                    # A bad key or a missing page fails the same way every time; the host is fine.
                    raise HttpClientError(f"{method} {url} returned {status}") from exc
                last_exc = exc
                self._breaker.record_failure()
                if attempt < self._max_retries:
                    backoff = 2**attempt * 0.5 + random.uniform(0, 0.25)  # noqa: S311
                    log_event(
                        logger,
                        logging.WARNING,
                        "http.retry",
                        url=url,
                        method=method,
                        attempt=attempt + 1,
                        status=getattr(getattr(exc, "response", None), "status_code", None),
                        backoff=round(backoff, 3),
                    )
                    sleep(backoff)
        raise HttpClientError(f"request failed after retries: {method} {url}") from last_exc

    def close(self) -> None:
        self._client.close()
