"""Public demo sandbox (Phase 7l Part B, NFR-ME-007, ADR-0050). Real Postgres; skips offline."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.auth.demo import DEMO_EMAIL, reset_demo_sandbox
from app.config import get_settings
from app.db import get_db
from app.main import app
from app.models import ChatMessage, ChatSession, Company, Document, Filing, User, Watchlist

_P = "/api/v1"


@pytest.fixture(scope="module")
def _migrated(test_db_url: str) -> None:
    from tests.integration.conftest import _MIGRATIONS_DIR

    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", test_db_url)
    command.upgrade(cfg, "head")


@pytest.fixture
def session(_migrated: None, test_engine: Engine) -> Iterator[Session]:
    with Session(test_engine) as s:
        yield s
        s.rollback()


@pytest.fixture
def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(session, "commit", session.flush)
    monkeypatch.setattr(get_settings(), "refresh_cookie_secure", False)
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _demo(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setattr(get_settings(), "demo_mode", on)
    # Demo mode forces rate limiting on; keep this test's handful of requests under the limit.
    monkeypatch.setattr(get_settings(), "rate_limit_per_minute", 1000)


def test_demo_endpoints_are_absent_outside_demo_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _demo(monkeypatch, False)
    assert client.post(f"{_P}/auth/demo").status_code == 404
    assert client.get(f"{_P}/meta/demo").json() == {"enabled": False, "data_as_of": None}


def test_demo_mode_disables_registration(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _demo(monkeypatch, True)
    resp = client.post(
        f"{_P}/auth/register", json={"email": "real@example.com", "password": "x" * 16}
    )
    assert resp.status_code == 403 and "disabled in the demo" in resp.json()["title"] + str(
        resp.json().get("detail")
    )


def test_shared_sandbox_signs_in_writes_and_resets(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _demo(monkeypatch, True)
    token = client.post(f"{_P}/auth/demo").json()["access_token"]
    me = client.get(f"{_P}/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["email"] == DEMO_EMAIL
    # A second visitor gets the same account.
    again = client.post(f"{_P}/auth/demo").json()["access_token"]
    assert (
        client.get(f"{_P}/auth/me", headers={"Authorization": f"Bearer {again}"}).json()["id"]
        == me["id"]
    )

    user_id = int(me["id"])
    session.add(Watchlist(user_id=user_id, name="Demo picks"))
    session.flush()

    assert reset_demo_sandbox(session)
    session.flush()
    session.expunge_all()
    assert session.scalar(select(User).where(User.id == user_id)) is None
    assert session.scalar(select(Watchlist).where(Watchlist.user_id == user_id)) is None
    assert not reset_demo_sandbox(session)  # nothing left to reset

    fresh = client.post(f"{_P}/auth/demo").json()["access_token"]
    new_me = client.get(f"{_P}/auth/me", headers={"Authorization": f"Bearer {fresh}"}).json()
    assert new_me["email"] == DEMO_EMAIL and int(new_me["id"]) != user_id


def test_demo_banner_reports_the_fixture_date(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _demo(monkeypatch, True)
    company = Company(ticker="DMO", cik="0000000099", name="Demo Corp")
    session.add(company)
    session.flush()
    for i, filed in enumerate((date(2026, 2, 25), date(2026, 5, 28))):
        doc = Document(
            company_id=company.id, document_type="filing", source_tier=1, content_hash=f"dm{i}"
        )
        session.add(doc)
        session.flush()
        session.add(
            Filing(
                document_id=doc.id,
                company_id=company.id,
                filing_type="10-Q",
                filing_date=filed,
                accession_number=f"0000000099-26-00000{i}",
            )
        )
    session.flush()
    body = client.get(f"{_P}/meta/demo").json()
    assert body["enabled"] is True and body["data_as_of"] >= "2026-05-28"


def test_demo_hides_saved_questions(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0050: every visitor shares one account, so one visitor's questions stay unseen."""
    _demo(monkeypatch, True)
    token = client.post(f"{_P}/auth/demo").json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    user_id = int(client.get(f"{_P}/auth/me", headers=headers).json()["id"])
    company = Company(ticker="DMQ", cik="0000000098", name="Demo Q Corp")
    session.add(company)
    session.flush()
    chat = ChatSession(user_id=user_id, company_id=company.id)
    session.add(chat)
    session.flush()
    session.add(ChatMessage(session_id=chat.id, role="user", content="my private question"))
    session.flush()

    assert client.get(f"{_P}/chat/questions", headers=headers).json() == []
