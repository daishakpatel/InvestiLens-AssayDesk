"""Citation source-resolution + persistence integration tests (Phase 3a DoD).

Real Postgres; skips offline. Seeds one row of each of the six source types and asserts
`resolve_source` / `GET /sources/{source_id}` return the right typed record (CIT-002, CIT-006), and
that verification outcomes persist to `claim_verifications` with reason codes (CIT-005 L6).
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

from app.citation.evidence import EvidenceItem, index_evidence
from app.citation.pipeline import persist, verify_text
from app.citation.resolver import resolve_source
from app.db import get_db
from app.main import app
from app.models import (
    ClaimVerification,
    Company,
    Document,
    DocumentChunk,
    FinancialFact,
    FinancialMetric,
    News,
    PriceHistory,
    ResearchReport,
)
from app.schemas.sources import TextChunkSource


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
def seeded(session: Session) -> dict[str, str]:
    """Seed one of each source type; return a map of source_type → source_id."""
    company = Company(ticker="TST", cik="0000000010", name="Test Corp")
    session.add(company)
    session.flush()

    filing_doc = Document(
        company_id=company.id,
        document_type="filing",
        source_tier=1,
        content_hash="f1",
        source_url="https://sec.gov/x",
    )
    er_doc = Document(
        company_id=company.id,
        document_type="earnings_release",
        source_tier=2,
        content_hash="e1",
        source_url="https://ir.example/er",
    )
    session.add_all([filing_doc, er_doc])
    session.flush()

    text_chunk = DocumentChunk(
        document_id=filing_doc.id,
        company_id=company.id,
        chunk_index=0,
        chunk_type="text",
        text="Revenue was $60,922 million for fiscal 2025.",
        section="Item 7",
        section_path=["Item 7", "Results of Operations"],
        paragraph_id="f_text_0",
        char_start=10,
        char_end=55,
        tier=1,
        parser_version="doc-parse-v1",
    )
    table_chunk = DocumentChunk(
        document_id=filing_doc.id,
        company_id=company.id,
        chunk_index=1,
        chunk_type="table",
        text="Consolidated Statements of Income",
        paragraph_id="f_table_1",
        tier=1,
        parser_version="doc-parse-v1",
        chunk_metadata={
            "table_id": "t1",
            "row_labels": ["Revenue"],
            "col_labels": ["FY2025"],
            "cell_refs": ["r0c0"],
        },
    )
    er_chunk = DocumentChunk(
        document_id=er_doc.id,
        company_id=company.id,
        chunk_index=0,
        chunk_type="text",
        text="We delivered record quarterly revenue.",
        paragraph_id="er_text_0",
        tier=2,
        parser_version="doc-parse-v1",
    )
    fact = FinancialFact(
        company_id=company.id,
        accession_number="0001045810-25-000023",
        concept_tag="us-gaap:Revenues",
        context_id="FY2025",
        value=Decimal("60922000000"),
        unit="USD",
    )
    derived = FinancialMetric(
        company_id=company.id,
        period="FY2025",
        period_type="FY",
        metric_name="gross_margin",
        metric_value=Decimal("0.71"),
        unit="ratio",
        is_derived=True,
        formula_id="gross_margin",
        source_id="derived:gross_margin:FY2025",
        quality_flags={
            "inputs": [{"name": "gross_profit", "source_id": "xbrl:1"}],
            "formula_version": "v1",
        },
    )
    news = News(
        company_id=company.id,
        title="Analyst note",
        description="Upgrade on AI demand",
        url="https://news.example/1",
        publisher="Reuters",
        published_at=datetime.now(UTC),
    )
    session.add_all([text_chunk, table_chunk, er_chunk, fact, derived, news])
    session.flush()

    return {
        "text_chunk": "f_text_0",
        "table_chunk": "f_table_1",
        "earnings_release": "er_text_0",
        "xbrl_fact": f"xbrl:{fact.id}",
        "derived_metric": "derived:gross_margin:FY2025",
        "news_item": f"news:{news.id}",
    }


def test_resolve_all_six_source_types(session: Session, seeded: dict[str, str]) -> None:
    for expected_type, source_id in seeded.items():
        detail = resolve_source(session, source_id)
        assert detail is not None, f"{expected_type} did not resolve"
        assert detail.source.source_type == expected_type


def test_text_chunk_marks_span_and_deep_link(session: Session, seeded: dict[str, str]) -> None:
    detail = resolve_source(session, seeded["text_chunk"])
    assert detail is not None
    assert detail.highlight_start == 10 and detail.highlight_end == 55
    assert detail.deep_link == "https://sec.gov/x"
    assert detail.text and "60,922" in detail.text


def test_derived_metric_exposes_lineage(session: Session, seeded: dict[str, str]) -> None:
    detail = resolve_source(session, seeded["derived_metric"])
    assert detail is not None
    assert detail.lineage == [{"name": "gross_profit", "source_id": "xbrl:1"}]  # CIT-003


def test_unknown_source_resolves_to_none(session: Session, seeded: dict[str, str]) -> None:
    assert resolve_source(session, "does_not_exist") is None


def test_get_sources_endpoint(session: Session, seeded: dict[str, str]) -> None:
    app.dependency_overrides[get_db] = lambda: session
    try:
        client = TestClient(app)
        resp = client.get(f"/api/v1/sources/{seeded['text_chunk']}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"]["source_type"] == "text_chunk"
        assert body["deep_link"] == "https://sec.gov/x"

        missing = client.get("/api/v1/sources/nope")
        assert missing.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_verifications_persist_with_reason_codes(session: Session, seeded: dict[str, str]) -> None:
    company = session.scalar(select(Company).where(Company.ticker == "TST"))
    assert company is not None
    report = ResearchReport(company_id=company.id, status="complete")
    session.add(report)
    session.flush()

    ev = index_evidence(
        [
            EvidenceItem(
                source=TextChunkSource(source_id="k", tier=1, document_id="d"),
                text="Revenue was $60,922 million for fiscal 2025.",
            )
        ]
    )
    out = verify_text("Revenue was $60,922 million [SOURCE:k]. Profit tripled [SOURCE:ghost].", ev)
    n = persist(session, out, report_id=report.id)
    assert n == 2
    session.flush()

    rows = list(
        session.scalars(select(ClaimVerification).where(ClaimVerification.report_id == report.id))
    )
    reasons = {r.reason_code for r in rows}
    assert "UNKNOWN_SOURCE" in reasons  # the ghost-cited claim logged with its reason (CIT-005 L6)
    assert any(r.status == "accepted" for r in rows)


def test_price_ids_resolve_to_market_data(session: Session) -> None:  # CIT-006, ADR-0054
    # One trading day (chat answers, valuations) and two (alerts, earnings reactions); exact days
    # only, so a weekend or a malformed ID is not-found rather than another day's price.
    company = Company(ticker="PRC", cik="0000000011", name="Price Corp")
    session.add(company)
    session.flush()
    for day, close in ((date(2026, 9, 28), "227.21"), (date(2026, 9, 29), "230.5")):
        session.add(
            PriceHistory(
                company_id=company.id,
                date=day,
                open=Decimal("230.97"),
                high=Decimal("232.82"),
                low=Decimal("227.03"),
                close=Decimal(close),
                volume=101494933,
                provider="tiingo",
            )
        )
    session.flush()

    one = resolve_source(session, "price:PRC:2026-09-28")
    assert one is not None and one.source.source_type == "price_bar" and one.source.tier == 4
    assert one.text == (
        "PRC on 2026-09-28: opened at $230.97, high $232.82, low $227.03, closed at $227.21; "
        "volume 101,494,933 shares (Tiingo end-of-day data)"
    )
    two = resolve_source(session, "price:PRC:2026-09-28:2026-09-29")
    assert two is not None and two.text == (
        "PRC closed at $227.21 on 2026-09-28 and $230.50 on 2026-09-29 (Tiingo end-of-day data)"
    )
    for missing in (
        "price:PRC",
        "price:PRC:2026-09-27",
        "price:PRC:not-a-date",
        "price:NOPE:2026-09-28",
    ):
        assert resolve_source(session, missing) is None
