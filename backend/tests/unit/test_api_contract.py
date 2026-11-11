"""API contract tests (spec §24, Phase 0c DoD). Offline — no DB or network.

Covers: the published OpenAPI matches the app (API-005 no silent contract drift), route
ordering (`/companies/search` before `/companies/{ticker}`), RFC 7807 error shape (API-001),
and the request-ID echo (API-007).
"""

from __future__ import annotations

import inspect
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import get_settings
from app.db import get_db
from app.main import API_V1_PREFIX, REQUEST_ID_HEADER, app
from app.schemas.chat import MAX_QUESTION_LENGTH, ChatRequest

_OPENAPI_PATH = Path(__file__).resolve().parents[3] / "docs" / "openapi.json"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_published_openapi_matches_app() -> None:
    """docs/openapi.json must match the app's generated spec (run `make openapi` if this fails)."""
    published = json.loads(_OPENAPI_PATH.read_text())
    current = json.loads(json.dumps(app.openapi(), sort_keys=True))
    assert published == current, "OpenAPI drift: regenerate with `make openapi`"


def test_every_spec_endpoint_group_is_registered() -> None:
    """Every §24.2 endpoint group appears in the spec."""
    paths = app.openapi()["paths"]
    required = [
        "/health",
        "/ready",
        "/meta/data-freshness/{ticker}",
        "/companies/search",
        "/companies/{ticker}",
        "/companies/{ticker}/financials",
        "/companies/{ticker}/metrics/{metric_name}/lineage",
        "/companies/{ticker}/valuation",
        "/companies/{ticker}/prices",
        "/companies/{ticker}/filings",
        "/companies/{ticker}/news",
        "/companies/{ticker}/research/latest",
        "/filings/{filing_id}",
        "/filings/{filing_id}/sections",
        "/research",
        "/research/jobs/{job_id}",
        "/research/jobs/{job_id}/events",
        "/research/{research_id}",
        "/sources/{source_id}",
        "/chat",
        "/chat/stream",
        "/auth/register",
        "/auth/login",
        "/watchlists",
        "/alerts",
        "/admin/ingestion-runs",
    ]
    for route in required:
        assert f"{API_V1_PREFIX}{route}" in paths, f"missing endpoint: {route}"


def test_search_resolves_before_ticker(client: TestClient) -> None:
    """`/companies/search` must hit the search handler, not the `{ticker}` handler. Only search
    requires `q`, so its validation error proves which handler matched without touching the DB;
    the responses themselves are covered in integration/test_api_endpoints."""
    resp = client.get(f"{API_V1_PREFIX}/companies/search")
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["field"] == "query.q"


def test_error_uses_rfc7807_shape(client: TestClient) -> None:
    resp = client.get(f"{API_V1_PREFIX}/no-such-route")  # routing 404: no DB touched
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert body["status"] == 404
    assert body["title"] == "Not Found"
    assert body["request_id"]  # API-007 populated in the problem body


def test_validation_error_is_problem_json(client: TestClient) -> None:
    resp = client.get(f"{API_V1_PREFIX}/companies/search")  # missing required ?q
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["errors"], "validation errors[] must be populated"


def test_request_id_is_echoed(client: TestClient) -> None:
    resp = client.get(f"{API_V1_PREFIX}/health", headers={REQUEST_ID_HEADER: "test-123"})
    assert resp.headers[REQUEST_ID_HEADER] == "test-123"  # API-007


def _session_dependencies(dependant: Any) -> list[Any]:
    """Every place in a route's dependency tree where `get_db` is requested."""
    own = [dependant] if dependant.call is get_db else []
    return own + [d for sub in dependant.dependencies for d in _session_dependencies(sub)]


def test_database_handlers_cannot_stall_the_api() -> None:
    """NFR-001, ADR-0029: sessions are synchronous and connections are few, so a handler that
    takes a session must (a) be a plain function, which FastAPI runs in a worker thread rather
    than on the event loop, and (b) give the session back when it returns
    (`scope="function"`), not while the request waits for a thread to serialise its response.
    Either mistake lets one resource be held while waiting for the other, and the API freezes."""
    on_the_loop, held_too_long = [], []
    # Included routers are nested, not flattened into `app.routes`, so walk them.
    routes = [c.route for c in iter_route_contexts(app.routes) if isinstance(c.route, APIRoute)]
    assert any(_session_dependencies(r.dependant) for r in routes), "no handlers were checked"
    for route in routes:
        sessions = _session_dependencies(route.dependant)
        name = f"{sorted(route.methods or [])} {route.path}"
        if sessions and inspect.iscoroutinefunction(route.endpoint):
            on_the_loop.append(name)
        if any(d.scope != "function" for d in sessions):
            held_too_long.append(name)
    assert on_the_loop == [], "declare these handlers with `def`, not `async def`"
    assert held_too_long == [], 'declare the session as `Depends(get_db, scope="function")`'


def test_chat_question_length_is_bounded() -> None:
    """SEC-002, SEC-008: the question goes to the model, so its size is capped at the API."""
    assert ChatRequest(company="NVDA", question="x" * MAX_QUESTION_LENGTH).question
    for bad in ["", "   ", "x" * (MAX_QUESTION_LENGTH + 1)]:
        with pytest.raises(ValidationError):
            ChatRequest(company="NVDA", question=bad)


def test_start_up_price_catch_up_runs_only_when_switched_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§28.3: the catch-up is a local stand-in for the schedule, so it is off unless asked for
    and never blocks start-up (it runs on its own thread)."""
    import app.main as main_module

    calls: list[str] = []
    monkeypatch.setattr(main_module, "_catch_up_prices", lambda: calls.append("ran"))
    with TestClient(app):
        pass
    assert calls == []  # default: off

    monkeypatch.setenv("REFRESH_PRICES_ON_STARTUP", "true")
    get_settings.cache_clear()
    try:
        with TestClient(app):
            pass
        for thread in threading.enumerate():
            if thread.name == "price-catch-up":
                thread.join(timeout=5)
        assert calls == ["ran"]
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
