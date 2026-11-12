"""Phase 4a endpoint-wiring integration tests (real Postgres; skips offline).

Seeds one company with metrics, a derived metric, prices, filings + chunks, news, and freshness
rows, then drives every wired endpoint through the ASGI app. Covers the DoD: real data for seed
companies, research single-flight via Idempotency-Key, cursor pagination, RFC 7807 errors, and
freshness metadata.

Refs: §24, API-001/002/003/008, ADR-0016/0017/0006.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.main import app
from app.models import (
    Company,
    DataFreshness,
    Document,
    DocumentChunk,
    Filing,
    FinancialFact,
    FinancialMetric,
    IngestionRun,
    News,
    PriceHistory,
    ReportFeedback,
    ResearchReport,
    User,
)
from tests.fakes import auth_headers

_REV = {2023: "26974000000", 2024: "60922000000", 2025: "130497000000"}


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
def seed(session: Session) -> dict[str, int]:
    admin = User(email="a@example.com", password_hash="x", role="admin")  # noqa: S106  # fixture
    user = User(email="u@example.com", password_hash="x", role="user")  # noqa: S106  # fixture
    company = Company(
        ticker="TST", cik="0000000011", name="Test Corp", exchange="NASDAQ", sector="Technology"
    )
    session.add_all([admin, user, company])
    session.flush()

    for fy, value in _REV.items():
        session.add(
            FinancialMetric(
                company_id=company.id,
                period=f"FY{fy}",
                fiscal_year=fy,
                period_type="FY",
                period_end=date(fy, 1, 31),
                metric_name="revenue",
                metric_value=Decimal(value),
                unit="USD",
                source_id=f"xbrl:TST:revenue:FY{fy}",
                is_latest=True,
            )
        )
    # A derived metric with lineage for the /lineage endpoint (CIT-003).
    session.add(
        FinancialMetric(
            company_id=company.id,
            period="FY2025",
            fiscal_year=2025,
            period_type="FY",
            metric_name="gross_margin",
            metric_value=Decimal("0.7107"),
            unit="ratio",
            is_derived=True,
            formula_id="gross_margin",
            source_id="derived:TST:gross_margin:FY2025",
            is_latest=True,
            quality_flags={
                "formula_version": "v1",
                "inputs": [
                    {"name": "gross_profit", "value": "92700000000", "source_id": "xbrl:TST:gp"},
                    {"name": "revenue", "value": "130497000000", "source_id": "xbrl:TST:rev"},
                ],
                "warnings": [],
            },
        )
    )
    session.add(
        PriceHistory(
            company_id=company.id,
            date=date(2025, 1, 2),
            close=Decimal("100.50"),
            adj_close=Decimal("100.50"),
            volume=1_000_000,
            provider="tiingo",
        )
    )

    doc = Document(company_id=company.id, document_type="filing", source_tier=1, content_hash="d1")
    session.add(doc)
    session.flush()
    for i in range(2):
        session.add(
            Filing(
                document_id=doc.id,
                company_id=company.id,
                filing_type="10-K",
                filing_date=date(2025 - i, 2, 26),
                period_end=date(2025 - i, 1, 31),
                accession_number=f"0001045810-25-00002{i}",
                primary_document_url=f"https://sec/{i}",
            )
        )
    session.add(
        DocumentChunk(
            document_id=doc.id,
            company_id=company.id,
            chunk_index=0,
            chunk_type="text",
            text="Revenue grew on data center demand.",
            section="Item 7",
            section_path=["Item 7"],
            char_start=0,
            char_end=35,
            parser_version="doc-parse-v1",
        )
    )
    for i in range(2):
        session.add(
            News(
                company_id=company.id,
                title=f"Headline {i}",
                description="Body.",
                url=f"https://n/{i}",
                publisher="Reuters",
                category="product",
                relevance_score=Decimal("0.9"),
                published_at=datetime(2025, 1, 10 + i, tzinfo=UTC),
            )
        )
    now = datetime.now(UTC)
    for src in ("sec", "price", "news"):
        session.add(
            DataFreshness(
                company_id=company.id,
                source=src,
                status="fresh",
                last_success_at=now,
                last_attempt_at=now,
            )
        )
    session.add(IngestionRun(source="sec", company_id=company.id, status="success", counts={}))
    session.flush()
    return {"admin": admin.id, "user": user.id, "company": company.id}


@pytest.fixture
def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # Endpoints call db.commit(); keep writes inside this test's transaction so the fixture
    # rollback cleans up and nothing leaks into the shared module DB.
    monkeypatch.setattr(session, "commit", session.flush)
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


_P = "/api/v1"


def test_company_profile_and_freshness(client: TestClient, seed: dict[str, int]) -> None:
    resp = client.get(f"{_P}/companies/TST")
    assert resp.status_code == 200
    body = resp.json()
    assert body["company"]["name"] == "Test Corp"
    assert body["freshness"]["source"] == "sec" and body["freshness"]["freshness_status"] == "fresh"


def test_search_and_unknown_company(client: TestClient, seed: dict[str, int]) -> None:
    hits = client.get(f"{_P}/companies/search", params={"q": "TST"}).json()
    assert hits and hits[0]["ticker"] == "TST" and hits[0]["score"] == 1.0
    assert client.get(f"{_P}/companies/ZZZZ").status_code == 404  # RFC 7807 404


def test_financials_real_values(client: TestClient, seed: dict[str, int]) -> None:
    resp = client.get(f"{_P}/companies/TST/financials", params={"metrics": "revenue"})
    series = resp.json()["series"][0]
    assert series["metric_name"] == "revenue"
    values = [Decimal(p["value"]) for p in series["points"]]
    assert Decimal("130497000000") in values  # exact Decimal, not faked


def test_financials_mark_sector_inapplicable_series(
    client: TestClient, session: Session, seed: dict[str, int]
) -> None:
    """DR-042: a bank's operating income is N/A for its sector, not an unreported value."""
    session.add(Company(ticker="BNK", cik="0000000099", name="Bank Corp", sector="Banks"))
    session.flush()
    params = {"metrics": "operating_income,free_cash_flow,revenue"}
    bank = client.get(f"{_P}/companies/BNK/financials", params=params).json()["series"]
    assert {s["metric_name"]: s["applicable"] for s in bank} == {
        "operating_income": False,
        "free_cash_flow": False,
        "revenue": True,
    }
    tech = client.get(f"{_P}/companies/TST/financials", params=params).json()["series"]
    assert all(s["applicable"] for s in tech)


def test_financials_split_adjust_per_share_history(
    client: TestClient, seed: dict[str, int], session: Session
) -> None:
    """DR-026 / ADR-0051: EPS filed before a 10:1 split is served on today's basis, with a note;
    the stored row keeps the as-filed figure its citation points at."""
    company_id = seed["company"]
    split = FinancialFact(
        company_id=company_id,
        accession_number="0000000011-24-000050",
        concept_tag="us-gaap:StockholdersEquityNoteStockSplitConversionRatio1",
        period_end=date(2024, 6, 10),
        value=Decimal(10),
        filed_date=date(2024, 8, 28),
    )
    session.add(split)
    for fy, eps, accession, filed in (
        (2024, "11.93", "0000000011-24-000010", date(2024, 2, 21)),
        (2025, "2.94", "0000000011-25-000010", date(2025, 2, 26)),
    ):
        session.add_all(
            [
                FinancialFact(
                    company_id=company_id,
                    accession_number=accession,
                    concept_tag="us-gaap:EarningsPerShareDiluted",
                    period_end=date(fy, 1, 31),
                    value=Decimal(eps),
                    filed_date=filed,
                ),
                FinancialMetric(
                    company_id=company_id,
                    period=f"FY{fy}",
                    fiscal_year=fy,
                    period_type="FY",
                    period_end=date(fy, 1, 31),
                    metric_name="eps_diluted",
                    metric_value=Decimal(eps),
                    unit="USD/shares",
                    source_id=f"xbrl:TST:eps_diluted:FY{fy}",
                    as_reported_accession=accession,
                    is_latest=True,
                ),
            ]
        )
    session.flush()

    points = client.get(f"{_P}/companies/TST/financials", params={"metrics": "eps_diluted"}).json()[
        "series"
    ][0]["points"]
    fy2024, fy2025 = points
    assert Decimal(fy2024["value"]) == Decimal("1.193")
    assert "as filed 11.93, divided by 10" in fy2024["formula"]
    assert fy2024["source_id"] == "xbrl:TST:eps_diluted:FY2024"  # still cites the filing
    assert Decimal(fy2025["value"]) == Decimal("2.94") and fy2025["formula"] is None


def test_metric_lineage(client: TestClient, seed: dict[str, int]) -> None:
    resp = client.get(
        f"{_P}/companies/TST/metrics/gross_margin/lineage", params={"period": "FY2025"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["formula_id"] == "gross_margin" and body["formula_version"] == "v1"
    assert {i["name"] for i in body["inputs"]} == {"gross_profit", "revenue"}
    assert (
        client.get(f"{_P}/companies/TST/metrics/revenue/lineage?period=FY1900").status_code == 404
    )


def test_valuation_and_prices(client: TestClient, seed: dict[str, int]) -> None:
    val = client.get(f"{_P}/companies/TST/valuation")
    assert val.status_code == 200 and isinstance(val.json()["metrics"], list)
    prices = client.get(f"{_P}/companies/TST/prices").json()
    point = prices["points"][0]
    assert Decimal(point["close"]) == Decimal("100.50") and point["volume"] == 1000000


def test_filings_pagination_cursor(client: TestClient, seed: dict[str, int]) -> None:
    first = client.get(f"{_P}/companies/TST/filings", params={"limit": 1})
    assert first.status_code == 200 and len(first.json()) == 1
    cursor = first.headers.get("X-Next-Cursor")
    assert cursor  # a second page exists
    second = client.get(f"{_P}/companies/TST/filings", params={"limit": 1, "cursor": cursor})
    assert len(second.json()) == 1
    assert first.json()[0]["filing_id"] != second.json()[0]["filing_id"]
    # Seeded ids rise as filing dates fall, so this checks date order, not id order.
    assert first.json()[0]["filing_date"] > second.json()[0]["filing_date"]
    assert client.get(f"{_P}/companies/TST/filings", params={"cursor": "@@bad"}).status_code == 422


def test_filing_detail_and_sections(client: TestClient, seed: dict[str, int]) -> None:
    fid = client.get(f"{_P}/companies/TST/filings").json()[0]["filing_id"]
    detail = client.get(f"{_P}/filings/{fid}")
    assert detail.status_code == 200 and detail.json()["company_ticker"] == "TST"
    sections = client.get(f"{_P}/filings/{fid}/sections").json()["sections"]
    assert sections[0]["section_path"] == ["Item 7"]
    assert client.get(f"{_P}/filings/999999").status_code == 404


def test_news_filters_and_body_cursor(client: TestClient, seed: dict[str, int]) -> None:
    resp = client.get(f"{_P}/companies/TST/news", params={"category": "product", "limit": 1})
    body = resp.json()
    assert len(body["items"]) == 1 and body["next_cursor"]
    assert body["items"][0]["category"] == "product"
    none_match = client.get(f"{_P}/companies/TST/news", params={"category": "legal"}).json()
    assert none_match["items"] == []


def test_news_is_newest_published_first_across_pages(  # §24.2, API-003
    client: TestClient, session: Session, seed: dict[str, int]
) -> None:
    # Finnhub delivers newest first, so the oldest article gets the highest id.
    for day in (20, 18, 15):
        session.add(
            News(
                company_id=seed["company"],
                title=f"Late {day}",
                category="product",
                relevance_score=Decimal("0.9"),
                published_at=datetime(2025, 1, day, tzinfo=UTC),
            )
        )
    session.add(
        News(
            company_id=seed["company"],
            title="Undated",
            category="product",
            relevance_score=Decimal("0.9"),
        )
    )
    session.flush()

    titles: list[str] = []
    cursor = None
    while True:
        params: dict[str, str | int] = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = client.get(f"{_P}/companies/TST/news", params=params).json()
        titles += [item["title"] for item in body["items"]]
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert titles == ["Late 20", "Late 18", "Late 15", "Headline 1", "Headline 0", "Undated"]


def test_news_hides_passing_mentions_by_default(  # §10.8
    client: TestClient, session: Session, seed: dict[str, int]
) -> None:
    session.add(
        News(
            company_id=seed["company"],
            title="Markets rise broadly",
            relevance_score=Decimal("0.1"),
            published_at=datetime(2025, 2, 1, tzinfo=UTC),
        )
    )
    session.flush()
    default = client.get(f"{_P}/companies/TST/news").json()["items"]
    everything = client.get(f"{_P}/companies/TST/news", params={"min_relevance": 0}).json()
    assert "Markets rise broadly" not in [item["title"] for item in default]
    assert everything["items"][0]["title"] == "Markets rise broadly"


def test_research_single_flight(client: TestClient, seed: dict[str, int]) -> None:
    h = auth_headers(seed["user"])
    assert client.post(f"{_P}/research", json={"ticker": "TST"}).status_code == 401  # ADR-0006
    first = client.post(
        f"{_P}/research", json={"ticker": "TST"}, headers={**h, "Idempotency-Key": "k1"}
    )
    assert first.status_code == 202
    r1, j1 = first.json()["research_id"], first.json()["job_id"]
    # Same idempotency key → same job (API-003).
    again = client.post(
        f"{_P}/research", json={"ticker": "TST"}, headers={**h, "Idempotency-Key": "k1"}
    )
    assert again.json()["job_id"] == j1 and again.json()["research_id"] == r1
    # No key, same company with an in-flight job → attaches to it (JOB-005).
    third = client.post(f"{_P}/research", json={"ticker": "TST"}, headers=h)
    assert third.json()["job_id"] == j1
    assert client.get(f"{_P}/research/jobs/{j1}").json()["status"] == "queued"
    report = client.get(f"{_P}/research/{r1}")
    assert report.status_code == 200 and report.json()["report"]["executive_summary"] == []
    assert client.get(f"{_P}/companies/TST/research/latest").status_code == 404
    assert client.post(f"{_P}/research", json={"ticker": "ZZZZ"}, headers=h).status_code == 404


def test_invalid_stored_report_is_409_not_500(
    client: TestClient, session: Session, seed: dict[str, int]
) -> None:
    """A stored report from an older schema is unreadable, not a server error (API-001)."""
    stale = ResearchReport(
        company_id=seed["company"],
        status="complete",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        report_json={"executive_summary": "not a list"},
    )
    session.add(stale)
    session.flush()
    for path in (f"{_P}/companies/TST/research/latest", f"{_P}/research/{stale.id}"):
        resp = client.get(path)
        assert resp.status_code == 409
        assert "generate a new one" in resp.json()["title"]


def _report_json(revenue: list[tuple[str, str]], **extra: object) -> dict[str, object]:
    """A minimal valid report whose revenue section cites (text, source_id) pairs."""
    empty: dict[str, object] = {
        key: []
        for key in (
            "executive_summary", "company_overview", "profitability_analysis",
            "balance_sheet_analysis", "cash_flow_analysis", "valuation_analysis",
            "news_summary", "risks", "management_commentary", "bull_factors", "bear_factors",
        )
    }  # fmt: skip
    claims = [
        {"claim_id": f"c{i}", "text": text, "source_ids": [sid]}
        for i, (text, sid) in enumerate(revenue)
    ]
    return {**empty, "revenue_analysis": claims, **extra}


def test_research_diff_against_previous_report(
    client: TestClient, session: Session, seed: dict[str, int]
) -> None:
    """§10.16 "what changed since last report": claims by cited evidence, metrics by period."""
    risk = {"category": "competitive", "description": "Rivals.", "source_ids": ["c_p1"]}
    old = ResearchReport(
        company_id=seed["company"],
        status="complete",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        data_version="a",
        prompt_version="report_v2",
        report_json=_report_json(
            [("Revenue was $60.9B.", "xbrl:TST:revenue:FY2024"),
             ("Revenue was $27.0B.", "xbrl:TST:revenue:FY2023")],
            risks=[risk],
        ),
    )  # fmt: skip
    new = ResearchReport(
        company_id=seed["company"],
        status="complete",
        generated_at=datetime(2026, 2, 1, tzinfo=UTC),
        data_version="b",
        prompt_version="report_v2",
        # Reworded but citing the same source: unchanged. Risks failed: skipped, not "removed".
        report_json=_report_json(
            [("Sales reached $60.9B.", "xbrl:TST:revenue:FY2024"),
             ("Revenue grew to $130.5B.", "xbrl:TST:revenue:FY2025")],
            failed_sections=["risks"],
        ),
    )  # fmt: skip
    session.add_all([old, new])
    session.flush()

    body = client.get(f"{_P}/research/{new.id}/diff").json()
    assert body["against_id"] == str(old.id)
    assert body["data_version_changed"] is True and body["prompt_version_changed"] is False
    assert body["skipped_sections"] == ["risks"]
    [section] = body["sections"]
    assert section["section"] == "revenue_analysis"
    assert [i["text"] for i in section["added"]] == ["Revenue grew to $130.5B."]
    assert [i["text"] for i in section["removed"]] == ["Revenue was $27.0B."]
    [metric] = body["metrics"]
    assert (metric["metric_name"], metric["period_type"]) == ("revenue", "FY")
    assert metric["before"]["period"] == "FY2024" and metric["after"]["period"] == "FY2025"
    assert Decimal(metric["after"]["value"]) == Decimal(_REV[2025])

    assert client.get(f"{_P}/research/{new.id}/diff", params={"against": old.id}).status_code == 200
    assert client.get(f"{_P}/research/{old.id}/diff").status_code == 404  # nothing earlier
    assert client.get(f"{_P}/research/{new.id}/diff", params={"against": new.id}).status_code == 404


def test_report_feedback_is_stored_once_per_reader(
    client: TestClient, session: Session, seed: dict[str, int]
) -> None:
    """§18.5: thumbs on a report or a claim, kept for evaluation; rating again replaces it."""
    report = ResearchReport(
        company_id=seed["company"],
        status="complete",
        report_json=_report_json([("Revenue was $60.9B.", "xbrl:TST:revenue:FY2024")]),
    )
    session.add(report)
    session.flush()
    url, h, rid = f"{_P}/feedback/report", auth_headers(seed["user"]), str(report.id)

    assert client.post(url, json={"report_id": rid, "rating": "up"}).status_code == 401
    assert (
        client.post(url, json={"report_id": "999999", "rating": "up"}, headers=h).status_code == 404
    )
    bad_claim = {"report_id": rid, "claim_id": "nope", "rating": "down"}
    assert client.post(url, json=bad_claim, headers=h).status_code == 404
    too_long = {"report_id": rid, "rating": "down", "reason": "x" * 2001}
    assert client.post(url, json=too_long, headers=h).status_code == 422

    assert client.post(url, json={"report_id": rid, "rating": "up"}, headers=h).status_code == 204
    again = {"report_id": rid, "rating": "down", "reason": " wrong period "}
    assert client.post(url, json=again, headers=h).status_code == 204
    claim = {"report_id": rid, "claim_id": "c0", "rating": "down"}
    assert client.post(url, json=claim, headers=h).status_code == 204

    rows = session.scalars(
        select(ReportFeedback).where(ReportFeedback.report_id == report.id)
    ).all()
    by_claim = {row.claim_id: (row.rating, row.reason) for row in rows}
    assert by_claim == {None: (-1, "wrong period"), "c0": (-1, None)}


def test_watchlist_and_alert_crud_requires_auth(client: TestClient, seed: dict[str, int]) -> None:
    assert client.post(f"{_P}/watchlists", json={"name": "Tech"}).status_code == 401
    headers = auth_headers(seed["user"])
    wl = client.post(f"{_P}/watchlists", json={"name": "Tech"}, headers=headers)
    assert wl.status_code == 201
    assert client.get(f"{_P}/watchlists", headers=headers).json()[0]["name"] == "Tech"
    assert client.delete(f"{_P}/watchlists/{wl.json()['id']}", headers=headers).status_code == 204

    al = client.post(
        f"{_P}/alerts", json={"ticker": "TST", "event_type": "new_filing"}, headers=headers
    )
    assert al.status_code == 201 and al.json()["ticker"] == "TST"
    assert len(client.get(f"{_P}/alerts", headers=headers).json()) == 1


def test_alert_and_watchlist_lists_are_user_scoped(
    client: TestClient, session: Session, seed: dict[str, int]
) -> None:
    """SEC-007/AUTH-004 IDOR: another user must never see this user's alerts or watchlists."""
    headers = auth_headers(seed["user"])
    assert (
        client.post(
            f"{_P}/alerts", json={"ticker": "TST", "event_type": "new_filing"}, headers=headers
        ).status_code
        == 201
    )
    assert (
        client.post(f"{_P}/watchlists", json={"name": "Mine"}, headers=headers).status_code == 201
    )

    bob = User(email="bob-idor@example.com", password_hash="x", role="user")  # noqa: S106
    session.add(bob)
    session.flush()
    bob_headers = auth_headers(bob.id)

    assert client.get(f"{_P}/alerts", headers=bob_headers).json() == []
    assert client.get(f"{_P}/watchlists", headers=bob_headers).json() == []


def test_admin_role_gated(client: TestClient, seed: dict[str, int]) -> None:
    user_h = auth_headers(seed["user"])
    admin_h = auth_headers(seed["admin"])
    assert client.get(f"{_P}/admin/ingestion-runs").status_code == 401  # no user
    assert client.get(f"{_P}/admin/ingestion-runs", headers=user_h).status_code == 403  # not admin
    runs = client.get(f"{_P}/admin/ingestion-runs", headers=admin_h)
    assert runs.status_code == 200 and runs.json()[0]["source"] == "sec"
    refresh = client.post(f"{_P}/admin/companies/TST/refresh", headers=admin_h)
    assert refresh.status_code == 202 and refresh.json()["status"] == "queued"


def test_data_freshness_meta(client: TestClient, seed: dict[str, int]) -> None:
    body = client.get(f"{_P}/meta/data-freshness/TST").json()
    assert {s["source"] for s in body["sources"]} == {"sec", "price", "news"}


def test_error_is_problem_json(client: TestClient, seed: dict[str, int]) -> None:
    resp = client.get(f"{_P}/companies/ZZZZ")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["status"] == 404 and resp.json()["request_id"]
