"""Alert dispatch and alert rules (§26.2 Phase 5d DoD; Phase 7a FR-AL-001..018). Real Postgres;
skips offline.

Phase 5d's DoD: "alert dispatch actually sends a notification within 30 minutes of a simulated
triggering event (e.g. a test filing ingestion)". Phase 7a adds: a new alert never replays older
events, each event is delivered exactly once (re-runs, overlapping runs, a failed watermark
update), one failing alert does not stop the rest, and large batches collapse into a digest.

Events inside one test transaction share Postgres' `now()`, which is also a new alert's
watermark, so tests that need an event *after* the alert give it an explicit later time.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.alerts import dispatch, registry
from app.alerts.dispatch import check_and_dispatch
from app.config import get_settings
from app.db import get_db
from app.main import app
from app.models import (
    Alert,
    Company,
    Document,
    Filing,
    News,
    Notification,
    PriceHistory,
    User,
    Watchlist,
    WatchlistItem,
)
from app.notifications.sender import EmailBackend, send_pending_emails
from app.observability.metrics import REGISTRY
from app.repositories import notifications as notif_repo
from app.repositories import prices as prices_repo
from tests.fakes import auth_headers

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
    monkeypatch.setattr(session, "commit", session.flush)  # keep writes in the test transaction
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _later(minutes: float) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


def _metric(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _company(session: Session, ticker: str = "TST") -> Company:
    company = Company(ticker=ticker, cik=f"{abs(hash(ticker)) % 10**10:010d}", name="Test Corp")
    session.add(company)
    session.flush()
    return company


def _user(session: Session, email: str = "alerts@example.com") -> User:
    user = User(email=email, password_hash="x")  # noqa: S106
    session.add(user)
    session.flush()
    return user


def _alert(session: Session, user: User, company: Company | None, **kwargs: object) -> Alert:
    alert = Alert(
        user_id=user.id,
        company_id=company.id if company else None,
        **{"event_type": "new_filing", **kwargs},
    )
    session.add(alert)
    session.flush()
    return alert


def _ingest_8k(
    session: Session, company: Company, *, at: datetime, n: int = 1, form: str = "8-K"
) -> Filing:
    doc = Document(
        company_id=company.id, document_type="filing", source_tier=1, content_hash=f"{n}{at}"
    )
    session.add(doc)
    session.flush()
    filing = Filing(
        document_id=doc.id,
        company_id=company.id,
        filing_type=form,
        filing_date=date(2026, 10, 8),
        accession_number=f"{company.cik}-26-{n:06d}",
    )
    filing.created_at = at
    session.add(filing)
    session.flush()
    return filing


def _notes(session: Session, alert: Alert) -> list[Notification]:
    return list(session.scalars(select(Notification).where(Notification.alert_id == alert.id)))


# --- Phase 5d behaviour, on the v2 framework ----------------------------------------------------


def test_new_filing_dispatches_notification(session: Session) -> None:
    """Phase 5d DoD + FR-AL-014: a filing after the alert is delivered, the latency histogram
    records it, and a second run sends nothing."""
    company = _company(session)
    user = _user(session)
    alert = _alert(session, user, company)
    filing = _ingest_8k(session, company, at=_later(1))  # the triggering event
    latency_count = _metric("alert_dispatch_latency_seconds_count")

    assert check_and_dispatch(session, now=_later(2)) == 1
    [notif] = _notes(session, alert)
    assert notif.channel == "in_app" and "8-K" in notif.title
    assert notif.event_key == f"new_filing:TST:{filing.accession_number}"
    assert notif.payload is not None and notif.payload["source_ids"] == [f"filing:{filing.id}"]
    assert _metric("alert_dispatch_latency_seconds_count") == latency_count + 1

    assert check_and_dispatch(session, now=_later(3)) == 0


def test_price_move_dispatches_when_over_threshold(session: Session) -> None:
    company = _company(session, "MOV")
    user = _user(session, "mover@example.com")
    alert = _alert(session, user, company, event_type="price_move", params={"threshold": 0.05})
    session.add_all(
        [
            PriceHistory(
                company_id=company.id,
                date=date(2026, 10, 7),
                close=Decimal("100.00"),
                provider="mock",
                ingested_at=_later(-60 * 24),
            ),
            PriceHistory(
                company_id=company.id,
                date=date(2026, 10, 8),
                close=Decimal("110.00"),
                provider="mock",
                ingested_at=_later(1),
            ),
        ]
    )
    session.flush()

    assert check_and_dispatch(session, now=_later(2)) == 1
    [notif] = _notes(session, alert)
    assert notif.title == "MOV moved +10.0%"
    assert notif.event_key == "price_move:MOV:2026-10-08"


def _bars(session: Session, company: Company, closes: dict[date, tuple[str, float]]) -> None:
    """Bars by date: (close, minutes from now when first stored)."""
    for day, (close, minutes) in closes.items():
        session.add(
            PriceHistory(
                company_id=company.id,
                date=day,
                close=Decimal(close),
                provider="mock",
                ingested_at=_later(minutes),
            )
        )
    session.flush()


def test_price_move_checks_every_day_a_run_catches_up(session: Session) -> None:
    """A run that stores two missed days checks both, each against the day before it."""
    company = _company(session, "CUP")
    alert = _alert(
        session,
        _user(session, "catchup@example.com"),
        company,
        event_type="price_move",
        params={"threshold": 0.05},
    )
    day_before = -60 * 24
    _bars(
        session,
        company,
        {
            date(2026, 10, 7): ("100.00", day_before),
            date(2026, 10, 8): ("110.00", 1),  # +10% on a day the old check never looked at
            date(2026, 10, 9): ("111.00", 1),  # +0.9%: under the threshold
        },
    )

    assert check_and_dispatch(session, now=_later(2)) == 1
    [notif] = _notes(session, alert)
    assert notif.event_key == "price_move:CUP:2026-10-08"


def test_price_move_ignores_history_and_a_first_load(session: Session) -> None:
    """Years of bars stored at once (a new company) count only their newest day, and an older
    date backfilled later is history, not news."""
    fresh, known = _company(session, "FRS"), _company(session, "OLD")
    user = _user(session, "history@example.com")
    first_load = _alert(session, user, fresh, event_type="price_move", params={"threshold": 0.05})
    backfill = _alert(session, user, known, event_type="price_move", params={"threshold": 0.05})
    _bars(
        session,
        fresh,
        {
            date(2026, 10, 7): ("100.00", 1),
            date(2026, 10, 8): ("150.00", 1),  # +50%, but part of the first load
            date(2026, 10, 9): ("151.00", 1),
        },
    )
    _bars(
        session,
        known,
        {
            date(2026, 10, 8): ("100.00", -60 * 24),
            date(2026, 10, 9): ("101.00", -60 * 24),
            date(2026, 10, 1): ("50.00", 1),  # an old date stored now: not a new move
            date(2026, 10, 2): ("100.00", 1),
        },
    )

    assert check_and_dispatch(session, now=_later(2)) == 0
    assert _notes(session, first_load) == [] and _notes(session, backfill) == []


def test_a_refetched_bar_keeps_when_it_was_first_stored(session: Session) -> None:
    """Every price run re-fetches years of bars; `ingested_at` must not move, or every old bar
    would look new to the price alert."""
    company = _company(session, "RFT")
    first = _later(-60)
    bar = {"company_id": company.id, "date": date(2026, 10, 7), "close": Decimal("10.00")}
    prices_repo.upsert_price_bars(session, company.id, [bar | {"ingested_at": first}])
    prices_repo.upsert_price_bars(
        session, company.id, [bar | {"close": Decimal("11.00"), "ingested_at": _later(0)}]
    )
    row = session.scalars(select(PriceHistory).where(PriceHistory.company_id == company.id)).one()
    session.refresh(row)
    assert row.close == Decimal("11.00") and row.ingested_at == first


def test_news_alert_filters_by_category(session: Session) -> None:
    """FR-AL-003: the news type fires per new item, limited to the chosen category."""
    company = _company(session, "NWS")
    user = _user(session, "news@example.com")
    alert = _alert(
        session, user, company, event_type="news_category", params={"category": "earnings"}
    )
    for title, category in (("Q3 results beat", "earnings"), ("New office opens", "general")):
        item = News(company_id=company.id, title=title, category=category)
        item.created_at = _later(1)
        session.add(item)
    session.flush()

    assert check_and_dispatch(session, now=_later(2)) == 1
    [notif] = _notes(session, alert)
    assert notif.title == "New earnings news for NWS" and notif.body == "Q3 results beat"


def test_email_goes_out_from_the_committed_outbox(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0064: dispatch only creates the notification; its email is sent afterwards, once, and
    a failed send stays pending for the next run instead of being lost or repeated (FR-AL-007)."""
    monkeypatch.setattr(session, "commit", session.flush)  # keep writes in the test transaction
    company = _company(session, "EML")
    user = _user(session, "emailme@example.com")
    _alert(session, user, company, channels=["in_app", "email"])
    _ingest_8k(session, company, at=_later(1))

    captured: list[tuple[str, str]] = []
    down = True

    class _FlakyBackend(EmailBackend):
        def send(self, *, to: str, subject: str, body: str) -> None:
            if down:
                raise ConnectionError("smtp down")
            captured.append((to, subject))

    assert check_and_dispatch(session, now=_later(2)) == 1
    assert captured == []  # nothing is emailed inside the dispatch transaction
    assert send_pending_emails(session, _FlakyBackend()) == 0  # provider down: stays pending
    [notif] = session.scalars(select(Notification).where(Notification.user_id == user.id)).all()
    assert notif.channel == "email" and notif.emailed_at is None

    down = False
    assert send_pending_emails(session, _FlakyBackend()) == 1
    assert captured == [("emailme@example.com", "New 8-K for EML")]
    assert notif.emailed_at is not None
    # Neither a re-run of the outbox nor of dispatch sends it again.
    assert send_pending_emails(session, _FlakyBackend()) == 0
    assert check_and_dispatch(session, now=_later(3)) == 0
    assert send_pending_emails(session, _FlakyBackend()) == 0
    assert len(captured) == 1


def test_notifications_endpoint_returns_dispatched(session: Session) -> None:
    """FR-AL-018: the API hands the client the ticker and evidence ids to link to."""
    company = _company(session, "API")
    user = _user(session, "apiuser@example.com")
    _alert(session, user, company)
    filing = _ingest_8k(session, company, at=_later(1))
    check_and_dispatch(session, now=_later(2))

    app.dependency_overrides[get_db] = lambda: session
    try:
        client = TestClient(app)
        resp = client.get(f"{_P}/notifications", headers=auth_headers(user.id))
        assert resp.status_code == 200
        [item] = resp.json()
        assert "8-K" in item["title"] and item["read"] is False
        assert item["ticker"] == "API" and item["event_type"] == "new_filing"
        assert item["source_ids"] == [f"filing:{filing.id}"] and item["severity"] == "info"
    finally:
        app.dependency_overrides.clear()


# --- Phase 7a: watermark and idempotency --------------------------------------------------------


def test_new_alert_does_not_replay_older_events(session: Session) -> None:
    """FR-AL-005 regression: a new alert on a ticker with an older matching filing fires zero
    notifications on its first run."""
    company = _company(session, "OLD")
    user = _user(session, "old@example.com")
    _ingest_8k(session, company, at=_later(-60 * 24 * 30))  # filed last month
    alert = _alert(session, user, company)

    assert check_and_dispatch(session) == 0
    assert _notes(session, alert) == []


def test_event_fires_once_when_the_watermark_update_fails(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-AL-006/008: notifications created before a failed watermark advance stay; the next run
    re-detects the event and delivers nothing new."""
    company = _company(session, "WMK")
    user = _user(session, "wmk@example.com")
    alert = _alert(session, user, company)
    _ingest_8k(session, company, at=_later(1))
    original_watermark = alert.watermark_at
    errors = _metric("alert_dispatch_errors_total", {"event_type": "new_filing"})

    def _fail(*_args: object) -> None:
        raise RuntimeError("watermark write failed")

    monkeypatch.setattr(dispatch, "_advance_watermark", _fail)
    assert check_and_dispatch(session, now=_later(2)) == 1
    assert alert.watermark_at == original_watermark  # not advanced
    assert _metric("alert_dispatch_errors_total", {"event_type": "new_filing"}) == errors + 1

    monkeypatch.undo()
    assert check_and_dispatch(session, now=_later(3)) == 0
    assert len(_notes(session, alert)) == 1
    assert alert.watermark_at > original_watermark


def test_alert_event_key_is_unique(session: Session) -> None:
    """FR-AL-007: the database itself refuses a second row for the same (alert, event_key)."""
    company = _company(session, "UNQ")
    user = _user(session, "unq@example.com")
    alert = _alert(session, user, company)
    fields = {
        "user_id": user.id,
        "alert_id": alert.id,
        "company_id": company.id,
        "event_key": "new_filing:UNQ:1",
        "title": "t",
        "body": "b",
        "channel": "in_app",
        "payload": {},
    }
    assert notif_repo.create_once(session, **fields) is not None  # type: ignore[arg-type]
    assert notif_repo.create_once(session, **fields) is None  # type: ignore[arg-type]
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(Notification(**fields))
        session.flush()


def test_overlapping_runs_deliver_an_event_once(
    session: Session, test_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-AL-007: two dispatch runs in separate sessions both detect the same event before either
    writes; the unique constraint lets exactly one notification through."""
    with Session(test_engine) as setup:
        company = _company(setup, "CON")
        user = _user(setup, "concurrent@example.com")
        alert = _alert(setup, user, company)
        _ingest_8k(setup, company, at=_later(1))
        setup.commit()
        alert_id, user_id, company_id = alert.id, user.id, company.id

    both_detected = threading.Barrier(2, timeout=20)
    original = registry.get("new_filing")
    assert original is not None

    def _detect_in_step(
        s: Session, a: Alert, since: datetime, until: datetime
    ) -> list[registry.DetectedEvent]:
        events = original.detector(s, a, since, until)
        if a.id == alert_id:
            both_detected.wait()
        return events

    monkeypatch.setitem(
        registry._REGISTRY, "new_filing", dataclasses.replace(original, detector=_detect_in_step)
    )
    created: list[int] = []
    failures: list[BaseException] = []

    def _run() -> None:
        try:
            with Session(test_engine) as s:
                check_and_dispatch(s, now=_later(2))
                s.commit()
            created.append(1)
        except BaseException as exc:  # surfaced below; a thread cannot fail the test itself
            failures.append(exc)

    try:
        threads = [threading.Thread(target=_run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not failures and len(created) == 2
        with Session(test_engine) as check:
            notes = check.scalars(select(Notification).where(Notification.alert_id == alert_id))
            assert len(list(notes)) == 1
    finally:
        with Session(test_engine) as cleanup:
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.execute(delete(Company).where(Company.id == company_id))
            cleanup.commit()


# --- Phase 7a: registry, isolation, digest ------------------------------------------------------


def test_failing_detector_does_not_stop_other_alerts(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-AL-013: a detector that raises is logged and counted; the next alert still fires."""

    def _boom(*_args: object) -> list[registry.DetectedEvent]:
        raise ValueError("detector bug")

    base = registry.get("new_filing")
    assert base is not None
    monkeypatch.setitem(
        registry._REGISTRY, "boom", dataclasses.replace(base, key="boom", detector=_boom)
    )
    company = _company(session, "BOM")
    user = _user(session, "boom@example.com")
    _alert(session, user, company, event_type="boom")  # lower id: evaluated first
    good = _alert(session, user, company)
    _ingest_8k(session, company, at=_later(1))
    errors = _metric("alert_dispatch_errors_total", {"event_type": "boom"})

    assert check_and_dispatch(session, now=_later(2)) == 1
    assert len(_notes(session, good)) == 1
    assert _metric("alert_dispatch_errors_total", {"event_type": "boom"}) == errors + 1


def test_unknown_event_type_is_skipped(session: Session) -> None:
    """FR-AL-004: a stored type that is no longer registered never crashes dispatch."""
    company = _company(session, "UNK")
    user = _user(session, "unk@example.com")
    alert = _alert(session, user, company, event_type="retired_type")
    assert check_and_dispatch(session) == 0
    assert _notes(session, alert) == []


def test_many_events_collapse_into_one_digest(session: Session) -> None:
    """FR-AL-015: more than 20 events in one run become one notification listing every key."""
    company = _company(session, "DIG")
    user = _user(session, "digest@example.com")
    alert = _alert(session, user, company)
    count = get_settings().alert_digest_threshold + 1
    for n in range(count):
        _ingest_8k(session, company, at=_later(1), n=n + 1)

    assert check_and_dispatch(session, now=_later(2)) == 1
    [digest] = _notes(session, alert)
    assert digest.title == f"{count} new filing alerts for DIG"
    assert digest.payload is not None and len(digest.payload["event_keys"]) == count
    assert digest.event_key is not None and digest.event_key.startswith("digest:")
    assert check_and_dispatch(session, now=_later(3)) == 0


def test_watchlist_wide_alert_reads_the_watchlist_at_run_time(session: Session) -> None:
    """FR-AL-011: a ticker added to the watchlist after the alert was created is covered."""
    user = _user(session, "wide@example.com")
    alert = _alert(session, user, None)
    company = _company(session, "WID")
    watchlist = Watchlist(user_id=user.id, name="Mine")
    session.add(watchlist)
    session.flush()
    session.add(WatchlistItem(watchlist_id=watchlist.id, company_id=company.id))
    _ingest_8k(session, company, at=_later(1))

    assert check_and_dispatch(session, now=_later(2)) == 1
    assert _notes(session, alert)[0].company_id == company.id


# --- Phase 7a: rules API ------------------------------------------------------------------------


def test_event_types_endpoint_lists_the_registry(client: TestClient) -> None:
    """FR-AL-010: the three legacy detectors (plus news) with JSON-Schema params."""
    resp = client.get(f"{_P}/alerts/event-types")
    assert resp.status_code == 200
    types = {t["key"]: t for t in resp.json()}
    assert {"new_filing", "price_move", "new_risk_factor", "news_category"} <= set(types)
    threshold = types["price_move"]["params_schema"]["properties"]["threshold"]
    assert threshold["type"] == "number" and threshold["default"] == 0.05


def test_alert_rule_crud(client: TestClient, session: Session) -> None:
    """FR-AL-009/010: create (ticker or watchlist-wide), list, toggle, edit params, delete."""
    _company(session, "CRD")
    headers = auth_headers(_user(session, "crud@example.com").id)

    made = client.post(
        f"{_P}/alerts",
        json={"event_type": "price_move", "ticker": "crd", "params": {"threshold": 0.1}},
        headers=headers,
    )
    assert made.status_code == 201, made.text
    alert = made.json()
    assert alert["ticker"] == "CRD" and alert["params"] == {"threshold": 0.1}
    assert alert["channels"] == ["in_app"] and alert["enabled"] and alert["supported"]

    wide = client.post(f"{_P}/alerts", json={"event_type": "new_filing"}, headers=headers)
    assert wide.status_code == 201
    assert wide.json()["ticker"] is None
    assert wide.json()["params"] == {"form_types": ["10-K", "10-Q", "8-K"]}  # defaults filled in

    patched = client.patch(
        f"{_P}/alerts/{alert['id']}",
        json={"enabled": False, "channels": ["email", "email"]},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False and patched.json()["channels"] == ["email"]
    assert len(client.get(f"{_P}/alerts", headers=headers).json()) == 2

    assert client.delete(f"{_P}/alerts/{alert['id']}", headers=headers).status_code == 204
    assert [a["id"] for a in client.get(f"{_P}/alerts", headers=headers).json()] == [
        wide.json()["id"]
    ]


def test_invalid_params_are_rejected_field_by_field(client: TestClient, session: Session) -> None:
    """FR-AL-009: params are validated against the type's model, errors name the field."""
    _company(session, "BAD")
    headers = auth_headers(_user(session, "bad@example.com").id)

    resp = client.post(
        f"{_P}/alerts",
        json={"event_type": "price_move", "ticker": "BAD", "params": {"threshold": 5}},
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
    assert [e["field"] for e in resp.json()["errors"]] == ["body.params.threshold"]

    extra = client.post(
        f"{_P}/alerts", json={"event_type": "new_filing", "params": {"x": 1}}, headers=headers
    )
    assert extra.status_code == 422 and extra.json()["errors"][0]["field"] == "body.params.x"

    unknown = client.post(f"{_P}/alerts", json={"event_type": "nope"}, headers=headers)
    assert unknown.status_code == 422
    assert unknown.json()["errors"][0]["field"] == "body.event_type"


def test_alerts_are_isolated_per_user(client: TestClient, session: Session) -> None:
    """FR-AL-011 / SEC-007: user B cannot read, edit or delete user A's alerts (404, not 403)."""
    _company(session, "ISO")
    alice = auth_headers(_user(session, "alice-alerts@example.com").id)
    bob = auth_headers(_user(session, "bob-alerts@example.com").id)
    alert_id = client.post(
        f"{_P}/alerts", json={"event_type": "new_filing", "ticker": "ISO"}, headers=alice
    ).json()["id"]

    assert client.get(f"{_P}/alerts", headers=bob).json() == []
    assert (
        client.patch(f"{_P}/alerts/{alert_id}", json={"enabled": False}, headers=bob).status_code
        == 404
    )
    assert client.delete(f"{_P}/alerts/{alert_id}", headers=bob).status_code == 404
    assert client.get(f"{_P}/alerts", headers=alice).json()[0]["enabled"] is True


def test_alert_limit_per_user(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-AL-012: past the per-user cap, creating another alert is a clear 422."""
    monkeypatch.setattr(get_settings(), "alert_max_per_user", 2)
    headers = auth_headers(_user(session, "limit@example.com").id)
    for _ in range(2):
        assert (
            client.post(f"{_P}/alerts", json={"event_type": "new_filing"}, headers=headers)
        ).status_code == 201
    resp = client.post(f"{_P}/alerts", json={"event_type": "new_filing"}, headers=headers)
    assert resp.status_code == 422 and "at most 2 alerts" in resp.json()["detail"]


def test_retired_event_type_is_listed_as_unsupported(client: TestClient, session: Session) -> None:
    """FR-AL-017: a rule whose type left the registry still shows, flagged, never disappears."""
    user = _user(session, "retired@example.com")
    _alert(session, user, None, event_type="retired_type")
    [alert] = client.get(f"{_P}/alerts", headers=auth_headers(user.id)).json()
    assert alert["event_type"] == "retired_type" and alert["supported"] is False
