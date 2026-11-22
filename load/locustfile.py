"""Load test for Assay Desk read endpoints + chat concurrency (Phase 5a, scope #8).

Verifies the §8 NFR latency targets against a RUNNING, seeded stack — it is not part of the
per-PR gate (ADR-0019). Run, record the result in docs/testing.md, and compare to the targets:

    NFR-001  GET /companies/{ticker}   p95 < 150 ms (cache hit) / < 500 ms (miss)
    FR-001   GET /companies/search      p95 < 300 ms (cache)
    NFR-002  retrieval (chat)           p95 < 400 ms
    NFR-004  chat first token           p95 < 3 s

Usage (needs the Locust dependency, not installed by default):
    uv run --with locust locust -f load/locustfile.py --host http://localhost:8000 \
        --users 50 --spawn-rate 5 --run-time 2m --headless

Set ASSAY_DESK_TOKEN to a Bearer access token to exercise the authenticated chat path
(ADR-0006: chat requires auth); without it, only the anonymous read endpoints are driven.
"""

from __future__ import annotations

import os
import random

from locust import HttpUser, between, task
from locust.exception import StopUser

_TICKERS = ["NVDA", "AAPL", "JPM"]
_PREFIXES = ["nvi", "app", "jpm", "nvda"]
_API = "/api/v1"


class ReadUser(HttpUser):
    """Anonymous read traffic: search + company profile + financials (the hot read paths)."""

    wait_time = between(0.5, 2.0)

    @task(3)
    def search(self) -> None:
        q = random.choice(_PREFIXES)  # noqa: S311 (non-crypto load sampling)
        self.client.get(f"{_API}/companies/search", params={"q": q}, name="search")

    @task(2)
    def company(self) -> None:
        t = random.choice(_TICKERS)  # noqa: S311
        self.client.get(f"{_API}/companies/{t}", name="company")

    @task(1)
    def financials(self) -> None:
        t = random.choice(_TICKERS)  # noqa: S311
        self.client.get(
            f"{_API}/companies/{t}/financials",
            params={"metrics": "revenue,net_income", "period_type": "FY"},
            name="financials",
        )


class ChatUser(HttpUser):
    """Authenticated chat concurrency (NFR-002/NFR-004). Only active when a token is provided."""

    wait_time = between(2.0, 5.0)

    def on_start(self) -> None:
        token = os.environ.get("ASSAY_DESK_TOKEN")
        if not token:
            raise StopUser()  # no auth → this user stops; the read users keep running
        self.client.headers.update({"Authorization": f"Bearer {token}"})

    @task
    def ask(self) -> None:
        t = random.choice(_TICKERS)  # noqa: S311
        self.client.post(
            f"{_API}/chat",
            json={"company": t, "question": "What drove revenue growth last year?"},
            name="chat",
        )
