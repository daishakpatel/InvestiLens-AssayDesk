"""Auth & user-feature integration tests (Phase 4b DoD). Real Postgres; skips offline.

Covers the full lifecycle (register → login → refresh → logout), hashed refresh tokens with
family-revoke on reuse, IDOR protection, the ADR-0006 anonymous policy, login throttling, account
deletion, and user-scoped CRUD.

Refs: §26, AUTH-001…006, SEC-007, ADR-0005/0006, LGL-007.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from app.auth import passwords, service
from app.auth.ratelimit import LoginThrottle
from app.config import get_settings
from app.db import get_chat_db, get_db
from app.main import app
from app.models import (
    Alert,
    ChatSession,
    Company,
    Job,
    RefreshToken,
    ResearchReport,
    User,
    Watchlist,
)

_P = "/api/v1"
_PW = "correct horse battery staple"


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
    # Endpoints commit; keep writes inside this test's transaction (flush) so rollback cleans up.
    monkeypatch.setattr(session, "commit", session.flush)
    # A fresh throttle per test so lockouts never leak across tests (the service uses a singleton).
    monkeypatch.setattr(service, "login_throttle", LoginThrottle())
    # TestClient talks http://, so the Secure refresh cookie would never be sent back; relax it.
    monkeypatch.setattr(get_settings(), "refresh_cookie_secure", False)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_chat_db] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def real_client(
    _migrated: None, test_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Each request gets its own session, as in production: what a handler does not commit is
    rolled back when the request ends. The shared-session `client` cannot show that."""
    from tests.integration.conftest import truncate_all

    monkeypatch.setattr(service, "login_throttle", LoginThrottle())
    monkeypatch.setattr(get_settings(), "refresh_cookie_secure", False)

    def _db() -> Iterator[Session]:
        with Session(test_engine) as s:
            yield s

    app.dependency_overrides[get_db] = _db

    app.dependency_overrides[get_chat_db] = _db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        truncate_all(test_engine)


def _register_and_login(client: TestClient, email: str) -> str:
    assert (
        client.post(f"{_P}/auth/register", json={"email": email, "password": _PW}).status_code
        == 201
    )
    resp = client.post(f"{_P}/auth/login", json={"email": email, "password": _PW})
    assert resp.status_code == 200
    return str(resp.json()["access_token"])


def test_full_lifecycle(client: TestClient, session: Session) -> None:
    access = _register_and_login(client, "flow@example.com")
    headers = {"Authorization": f"Bearer {access}"}
    me = client.get(f"{_P}/auth/me", headers=headers)
    assert me.status_code == 200 and me.json()["email"] == "flow@example.com"

    refreshed = client.post(f"{_P}/auth/refresh")  # refresh cookie travels with the client
    assert refreshed.status_code == 200 and refreshed.json()["access_token"]

    assert client.post(f"{_P}/auth/logout").status_code == 204
    # After logout the family is revoked, so a further refresh fails (AUTH-002).
    assert client.post(f"{_P}/auth/refresh").status_code == 401


def test_password_and_refresh_tokens_are_hashed(client: TestClient, session: Session) -> None:
    _register_and_login(client, "hash@example.com")
    user = session.scalar(select(User).where(User.email == "hash@example.com"))
    assert user is not None and user.password_hash.startswith("$argon2id$")  # AUTH-001
    token = session.scalar(select(RefreshToken).where(RefreshToken.user_id == user.id))
    assert token is not None and len(token.token_hash) == 64 and _PW not in token.token_hash


def test_registration_sets_default_ai_budget(client: TestClient, session: Session) -> None:
    """NFR-008/ADR-0022: a new user gets a real monthly AI budget, not an unenforced NULL."""
    _register_and_login(client, "budgeted@example.com")
    user = session.scalar(select(User).where(User.email == "budgeted@example.com"))
    assert user is not None
    assert user.ai_budget_month_usd == get_settings().default_ai_budget_month_usd


def test_refresh_reuse_revokes_family(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rotated-then-reused refresh token revokes the whole family (theft detection, AUTH-002)."""
    monkeypatch.setattr(get_settings(), "refresh_reuse_grace_seconds", 0)
    user = service.register(session, email="reuse@example.com", password=_PW)
    _, issued = service.login(
        session, email="reuse@example.com", password=_PW, user_agent=None, ip=None
    )
    stolen = issued.refresh_raw
    # Legitimate rotation: `stolen` is now revoked, a new token is live.
    _, rotated = service.refresh(session, raw_refresh=stolen, user_agent=None, ip=None)
    # Replaying the old (revoked) token is reuse → the family is revoked.
    with pytest.raises(service.InvalidToken):
        service.refresh(session, raw_refresh=stolen, user_agent=None, ip=None)
    # ...which also kills the legitimately-rotated token.
    with pytest.raises(service.InvalidToken):
        service.refresh(session, raw_refresh=rotated.refresh_raw, user_agent=None, ip=None)
    active = session.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
        )
    ).all()
    assert active == []


def test_idor_watchlist_and_chat_session(client: TestClient, session: Session) -> None:
    a_access = _register_and_login(client, "alice@example.com")
    a = session.scalar(select(User).where(User.email == "alice@example.com"))
    assert a is not None
    company = Company(ticker="TST", cik="0000000011", name="Test Corp")
    session.add(company)
    session.flush()
    a_headers = {"Authorization": f"Bearer {a_access}"}
    wl = client.post(f"{_P}/watchlists", json={"name": "Alice list"}, headers=a_headers)
    wl_id = wl.json()["id"]
    a_chat = ChatSession(user_id=a.id, company_id=company.id)
    session.add(a_chat)
    session.flush()

    b_access = _register_and_login(client, "bob@example.com")
    b_headers = {"Authorization": f"Bearer {b_access}"}
    # Bob sees none of Alice's data and cannot reach it by id (SEC-007 / AUTH-004 IDOR).
    assert client.get(f"{_P}/watchlists", headers=b_headers).json() == []
    assert client.delete(f"{_P}/watchlists/{wl_id}", headers=b_headers).status_code == 404
    assert client.get(f"{_P}/chat/sessions/{a_chat.id}", headers=b_headers).status_code == 404
    # Alice still owns her session.
    assert client.get(f"{_P}/chat/sessions/{a_chat.id}", headers=a_headers).status_code == 200


def test_anonymous_policy_adr0006(client: TestClient, session: Session) -> None:
    session.add(Company(ticker="TST", cik="0000000011", name="Test Corp"))
    session.flush()
    # Read-only browsing is allowed anonymously...
    assert client.get(f"{_P}/companies/TST").status_code == 200
    # ...but AI generation, chat, and user-owned data are not (anonymous AI quota = 0).
    assert client.post(f"{_P}/research", json={"ticker": "TST"}).status_code == 401
    assert client.post(f"{_P}/chat", json={"company": "TST", "question": "hi"}).status_code == 401
    assert client.get(f"{_P}/watchlists").status_code == 401


def test_login_is_throttled(client: TestClient, session: Session) -> None:
    client.post(f"{_P}/auth/register", json={"email": "lock@example.com", "password": _PW})
    for _ in range(get_settings().auth_login_max_attempts):
        bad = client.post(
            f"{_P}/auth/login", json={"email": "lock@example.com", "password": "nope-nope-nope"}
        )
        assert bad.status_code == 401
    # Further attempts are locked out, even with the correct password (AUTH-006).
    locked = client.post(f"{_P}/auth/login", json={"email": "lock@example.com", "password": _PW})
    assert locked.status_code == 429


def test_delete_me_soft_deletes(client: TestClient, session: Session) -> None:
    access = _register_and_login(client, "gone@example.com")
    headers = {"Authorization": f"Bearer {access}"}
    assert client.delete(f"{_P}/auth/me", headers=headers).status_code == 204
    user = session.scalar(select(User).where(User.email == "gone@example.com"))
    assert user is not None and user.deleted_at is not None and not user.is_active  # LGL-007
    # The access token no longer resolves to an active user, and login is refused.
    assert client.get(f"{_P}/auth/me", headers=headers).status_code == 401
    assert (
        client.post(
            f"{_P}/auth/login", json={"email": "gone@example.com", "password": _PW}
        ).status_code
        == 401
    )


def test_hard_delete_cascades_but_keeps_public_reports(
    client: TestClient, session: Session
) -> None:
    """SEC-014/LGL-007: the retention-job path must remove all of a user's owned data, but a
    research report they generated stays (public per ADR-0006) with its `user_id` nulled, not
    deleted — and the same for a background job's `created_by`."""
    access = _register_and_login(client, "purge@example.com")
    user = session.scalar(select(User).where(User.email == "purge@example.com"))
    assert user is not None
    company = session.scalar(select(Company).where(Company.ticker == "TST"))
    if company is None:
        company = Company(ticker="TST", cik="0000000012", name="Test Corp")
        session.add(company)
        session.flush()

    chat = ChatSession(user_id=user.id, company_id=company.id)
    watchlist = Watchlist(user_id=user.id, name="Mine")
    alert = Alert(user_id=user.id, company_id=company.id, event_type="new_filing")
    report = ResearchReport(company_id=company.id, user_id=user.id, status="complete")
    job = Job(job_type="research_report", params={}, created_by=user.id)
    session.add_all([chat, watchlist, alert, report, job])
    session.flush()
    report_id, job_id = report.id, job.id
    refresh_token_exists = (
        session.scalar(select(RefreshToken).where(RefreshToken.user_id == user.id)) is not None
    )
    assert refresh_token_exists, "login should have issued a refresh token"

    chat_id, watchlist_id, alert_id, user_id = chat.id, watchlist.id, alert.id, user.id
    assert service.hard_delete_user(session, user.id)
    session.flush()
    # The DB-level ON DELETE CASCADE removed these rows without the ORM's unit-of-work knowing,
    # so query fresh by id (bypassing the stale identity map) rather than `.get()` the old objects.
    session.expunge_all()

    assert session.scalar(select(User).where(User.id == user_id)) is None
    assert session.scalar(select(ChatSession).where(ChatSession.id == chat_id)) is None
    assert session.scalar(select(Watchlist).where(Watchlist.id == watchlist_id)) is None
    assert session.scalar(select(Alert).where(Alert.id == alert_id)) is None
    assert session.scalar(select(RefreshToken).where(RefreshToken.user_id == user_id)) is None

    kept_report = session.scalar(select(ResearchReport).where(ResearchReport.id == report_id))
    assert kept_report is not None and kept_report.user_id is None  # public report survives

    kept_job = session.scalar(select(Job).where(Job.id == job_id))
    assert kept_job is not None and kept_job.created_by is None

    # The access token minted before deletion no longer resolves to anyone.
    headers = {"Authorization": f"Bearer {access}"}
    assert client.get(f"{_P}/auth/me", headers=headers).status_code == 401


def test_email_verification_and_password_reset(client: TestClient, session: Session) -> None:
    user = service.register(session, email="verify@example.com", password=_PW)
    raw = service.request_email_verification(session, user)
    verified = service.confirm_email_verification(session, raw=raw)
    assert verified.email_verified_at is not None  # AUTH-003
    # Single use: the same token cannot be replayed.
    with pytest.raises(service.InvalidToken):
        service.confirm_email_verification(session, raw=raw)

    reset_raw = service.request_password_reset(session, email="verify@example.com")
    assert reset_raw is not None
    service.confirm_password_reset(
        session,
        raw=reset_raw,
        new_password="a brand new password 1",  # noqa: S106  # test fixture
    )
    refreshed = session.scalar(select(User).where(User.email == "verify@example.com"))
    assert refreshed is not None
    assert passwords.verify_password("a brand new password 1", refreshed.password_hash)
    # No account → neutral None (no user enumeration).
    assert service.request_password_reset(session, email="nobody@example.com") is None


def _refresh_with(client: TestClient, token: str) -> int:
    """POST /auth/refresh presenting `token`, whatever cookie the client currently holds."""
    client.cookies.clear()
    cookie = f"{get_settings().refresh_cookie_name}={token}"
    return int(client.post(f"{_P}/auth/refresh", headers={"Cookie": cookie}).status_code)


def test_reuse_revocation_outlives_the_rejected_request(
    real_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AUTH-002: replaying a rotated token ends the session for good. The revocation is written
    by the request that is rejected, so it must be committed, not rolled back with the 401."""
    monkeypatch.setattr(get_settings(), "refresh_reuse_grace_seconds", 0)
    name = get_settings().refresh_cookie_name
    _register_and_login(real_client, "durable@example.com")
    stolen = real_client.cookies[name]
    assert real_client.post(f"{_P}/auth/refresh").status_code == 200
    rotated = real_client.cookies[name]

    assert _refresh_with(real_client, stolen) == 401  # reuse detected
    assert _refresh_with(real_client, rotated) == 401  # and the family stays revoked


def test_two_tabs_refreshing_at_once_both_stay_signed_in(
    real_client: TestClient, test_engine: Engine
) -> None:
    """AUTH-002, ADR-0030: tabs restoring a session together present the same cookie. The second
    arrives just after the first has rotated it; within the grace window that is not theft."""
    name = get_settings().refresh_cookie_name
    _register_and_login(real_client, "tabs@example.com")
    shared = real_client.cookies[name]
    assert real_client.post(f"{_P}/auth/refresh").status_code == 200  # first tab
    first_tab = real_client.cookies[name]

    assert _refresh_with(real_client, shared) == 200  # second tab, same cookie
    second_tab = real_client.cookies[name]
    assert second_tab not in (shared, first_tab)
    assert _refresh_with(real_client, first_tab) == 200  # the first tab's session still works

    # The window is short: the same replay once it has passed is reuse, and ends every session.
    with Session(test_engine) as s:
        s.execute(
            update(RefreshToken)
            .where(RefreshToken.replaced_by_id.is_not(None))
            .values(revoked_at=RefreshToken.revoked_at - timedelta(minutes=1))
        )
        s.commit()
    assert _refresh_with(real_client, shared) == 401
    assert _refresh_with(real_client, second_tab) == 401


def test_a_rotated_token_cannot_revive_a_logged_out_session(real_client: TestClient) -> None:
    """AUTH-002: the grace window never outlives the session. After logout the family is
    revoked, and a token rotated a moment earlier gets nothing."""
    name = get_settings().refresh_cookie_name
    _register_and_login(real_client, "bye@example.com")
    old = real_client.cookies[name]
    assert real_client.post(f"{_P}/auth/refresh").status_code == 200
    assert real_client.post(f"{_P}/auth/logout").status_code == 204
    assert _refresh_with(real_client, old) == 401
