"""Valuation metrics from prices + filings (§10.7, §12, FR-024). Real Postgres, mock providers.

Refs: FR-024 (every multiple reproducible from stored inputs), DR-025/026 (raw close, share
counts moved across splits), DR-042 (bank EV multiples are N/A, never zero).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.citation import resolver
from app.citation.resolver import resolve_source
from app.finance.builder import build_company_metrics
from app.finance.ratio_builder import build_company_ratios
from app.finance.valuation_builder import (
    LATEST,
    VALUATION_METRICS,
    build_company_valuation,
    share_counts,
)
from app.ingestion.prices import ingest_prices
from app.ingestion.sec import ingest_company
from app.ingestion.sec.pipeline import SHARES_CONCEPT, _cover_share_rows
from app.models import Company, FinancialFact, FinancialMetric, PriceHistory
from app.providers.prices.mock import MockPriceSource
from app.providers.sec.mock import MockSecSource
from app.repositories import facts as facts_repo
from app.repositories import metrics as metric_repo
from app.repositories.filings import list_filings_by_type
from app.utils.storage import FilesystemObjectStorage


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


def _company(session: Session, ticker: str, tmp_path: Path) -> Company:
    """Filings and metrics first, then prices: the price run itself must build the valuation."""
    ingest_company(session, ticker, sec=MockSecSource(), storage=FilesystemObjectStorage(tmp_path))
    company = session.scalars(select(Company).where(Company.ticker == ticker)).one()
    build_company_metrics(session, company)
    build_company_ratios(session, company)
    ingest_prices(session, company, source=MockPriceSource())
    return company


def _rows(session: Session, company: Company, name: str) -> list[FinancialMetric]:
    return list(
        session.scalars(
            select(FinancialMetric).where(
                FinancialMetric.company_id == company.id, FinancialMetric.metric_name == name
            )
        )
    )


def test_every_multiple_is_reproducible_from_its_stored_inputs(
    session: Session, tmp_path: Path
) -> None:  # FR-024, DR-025
    nvda = _company(session, "NVDA", tmp_path)
    caps = [r for r in _rows(session, nvda, "market_cap") if r.period_type == "FY"]
    assert caps, "a fiscal year inside the price history should be valued"
    for row in caps:
        basis = (row.quality_flags or {})["basis"]
        assert row.metric_value == Decimal(basis["price_close"]) * Decimal(basis["shares"])
        assert basis["price_date"] <= str(row.period_end)
        assert row.source_id == f"derived:NVDA:market_cap:{row.period}"

    pe = next(r for r in _rows(session, nvda, "pe_ratio") if r.period == caps[-1].period)
    eps = metric_repo.get_latest_metric(
        session, company_id=nvda.id, metric_name="eps_diluted", period=pe.period
    )
    assert eps is not None and eps.metric_value
    basis = (pe.quality_flags or {})["basis"]
    expected = Decimal(basis["price_close"]) / eps.metric_value
    # The lineage points at the EPS row it used, so the citation panel can open it (CIT-003).
    assert {"name": "eps_diluted_ttm", "source_id": eps.source_id} in (pe.quality_flags or {})[
        "inputs"
    ]
    assert pe.metric_value == expected.quantize(Decimal("0.00000001"))  # NUMERIC(28, 8)
    assert pe.unit == "x"


def test_the_latest_close_is_what_the_header_reads(session: Session, tmp_path: Path) -> None:
    nvda = _company(session, "NVDA", tmp_path)
    latest = metric_repo.get_latest_metrics(
        session, company_id=nvda.id, metric_names=["market_cap", "pe_ratio"]
    )
    assert [r.period_type for r in latest] == [LATEST, LATEST]
    cap = latest[0]
    assert cap.period == "2026-09-30"  # the last bar in the price fixture
    assert cap.source_id == "derived:NVDA:market_cap:2026-09-30"
    lineage = (cap.quality_flags or {})["inputs"]
    assert any(i["source_id"] and i["source_id"].startswith("xbrl:") for i in lineage)

    # Rebuilding replaces rather than piles up: one latest row per multiple.
    build_company_valuation(session, nvda)
    for name in VALUATION_METRICS:
        assert sum(r.period_type == LATEST for r in _rows(session, nvda, name)) == 1


def test_bank_enterprise_value_multiples_are_not_applicable(
    session: Session, tmp_path: Path
) -> None:  # DR-042
    jpm = _company(session, "JPM", tmp_path)
    by_name = {
        r.metric_name: r
        for r in metric_repo.get_latest_metrics(
            session, company_id=jpm.id, metric_names=list(VALUATION_METRICS)
        )
    }
    for name in ("enterprise_value", "ev_revenue", "ev_ebitda"):
        assert by_name[name].metric_value is None
        assert "NOT_APPLICABLE_SECTOR" in str((by_name[name].quality_flags or {})["warnings"])
    assert by_name["market_cap"].metric_value and by_name["pe_ratio"].metric_value


def test_a_company_without_prices_gets_no_valuation(session: Session, tmp_path: Path) -> None:
    # Nothing to price against: no rows, rather than market caps of zero (DR-041).
    ingest_company(session, "AAPL", sec=MockSecSource(), storage=FilesystemObjectStorage(tmp_path))
    aapl = session.scalars(select(Company).where(Company.ticker == "AAPL")).one()
    build_company_metrics(session, aapl)
    build_company_ratios(session, aapl)
    assert build_company_valuation(session, aapl) == 0
    assert _rows(session, aapl, "market_cap") == []


def test_the_citation_panel_lists_the_price_without_a_dead_link(
    session: Session, tmp_path: Path
) -> None:  # CIT-003
    nvda = _company(session, "NVDA", tmp_path)
    pe = next(r for r in _rows(session, nvda, "pe_ratio") if r.period_type == LATEST)
    detail = resolve_source(session, pe.source_id or "")
    assert detail is not None
    # Since Phase 7h the latest P/E is market cap / TTM net income (ADR-0046): market cap is
    # listed without a dead link, and the net income cites the fact it came from.
    assert {"name": "market_cap"} in detail.lineage
    income = next(i for i in detail.lineage if i["name"] == "net_income_ttm")
    assert income["source_id"].startswith("xbrl:")
    pe = next(r for r in _rows(session, nvda, "pe_ratio") if r.period_type == LATEST)
    assert (pe.quality_flags or {})["basis"]["figures"].startswith("TTM to ")
    assert (detail.text or "").startswith("Price-to-earnings ratio as of ")


def test_a_replaced_latest_close_citation_still_resolves(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # CIT-002, FR-024, ADR-0063
    """A report cites the latest-close P/E; the next price run replaces that row. The dated ID
    must still open, with the value the report saw, rebuilt from what was known that day."""
    # The price fixture ends before the real TTM cut-over date; move it before the fixture.
    monkeypatch.setattr(resolver, "LATEST_TTM_SINCE", date(2026, 1, 1))
    nvda = _company(session, "NVDA", tmp_path)
    cited = {
        r.metric_name: (r.source_id, r.metric_value)
        for name in ("pe_ratio", "market_cap")
        for r in _rows(session, nvda, name)
        if r.period_type == LATEST
    }
    assert cited["pe_ratio"][0] == "derived:NVDA:pe_ratio:2026-09-30"

    session.add(
        PriceHistory(
            company_id=nvda.id,
            date=date(2026, 10, 1),
            close=Decimal("999.00"),
            adj_close=Decimal("999.00"),
            volume=1,
            provider="mock",
        )
    )
    session.flush()
    build_company_valuation(session, nvda)
    latest = next(r for r in _rows(session, nvda, "pe_ratio") if r.period_type == LATEST)
    assert latest.source_id == "derived:NVDA:pe_ratio:2026-10-01"  # the old row is gone

    for source_id, value in cited.values():
        detail = resolve_source(session, source_id or "")
        assert detail is not None, source_id
        assert detail.source.value == value  # type: ignore[union-attr]  # derived_metric
        assert detail.lineage
    # Only a stored trading day was ever a latest close; other metrics keep their own schemes.
    assert resolve_source(session, "derived:NVDA:pe_ratio:2026-09-27") is None  # a Sunday
    assert resolve_source(session, "derived:NVDA:roe:2026-09-30") is None

    # An ID from before the latest-close method switched to TTM is not rebuilt: its value would
    # contradict the report that quoted it.
    monkeypatch.setattr(resolver, "LATEST_TTM_SINCE", date(2026, 10, 24))
    assert resolve_source(session, cited["pe_ratio"][0] or "") is None


_CLASS = "us-gaap:StatementClassOfStockAxis"
_COVER_DAY = date(2019, 1, 2)  # no fixture filing reports a count on this day


def _class_fact(ref: str, member: str, shares: str) -> str:
    """One cover-page count for one share class, with its context (inline XBRL)."""
    return (
        f'<xbrli:context id="{ref}"><xbrli:segment><xbrldi:explicitMember dimension="{_CLASS}">'
        f"us-gaap:{member}</xbrldi:explicitMember></xbrli:segment><xbrli:period>"
        f"<xbrli:instant>{_COVER_DAY}</xbrli:instant></xbrli:period></xbrli:context>"
        f'<ix:nonFraction name="{SHARES_CONCEPT}" contextRef="{ref}" unitRef="shares">'
        f"{shares}</ix:nonFraction>"
    )


_TWO_CLASS_COVER = (
    "<html><body>"
    + _class_fact("cA", "CommonClassAMember", "2,000")
    + _class_fact("cB", "CommonClassBMember", "500")
    + "</body></html>"
)


def test_per_class_cover_counts_are_read_and_summed(session: Session, tmp_path: Path) -> None:
    """companyfacts drops per-class facts, so a multi-class filer's cover counts come from the
    filing's inline XBRL; valuation sums the classes unless they convert unevenly (DR-025,
    ADR-0074)."""
    storage = FilesystemObjectStorage(tmp_path)
    ingest_company(session, "NVDA", sec=MockSecSource(), storage=storage)
    company = session.scalars(select(Company).where(Company.ticker == "NVDA")).one()
    filing = list_filings_by_type(session, company_id=company.id, filing_type="10-K")[-1]
    assert filing.raw_document_location
    storage.put(filing.raw_document_location, _TWO_CLASS_COVER.encode())

    rows = [
        r
        for r in _cover_share_rows(session, company.id, [], storage)
        if r["accession_number"] == filing.accession_number
    ]
    assert sorted((r["dimensions"][_CLASS], r["value"]) for r in rows) == [
        ("us-gaap:CommonClassAMember", Decimal(2000)),
        ("us-gaap:CommonClassBMember", Decimal(500)),
    ]
    assert all(r["filed_date"] == filing.filing_date for r in rows)  # point in time (DR-023)
    # A filing companyfacts already counts is not read again.
    counted = [{"accession_number": filing.accession_number, "concept_tag": SHARES_CONCEPT}]
    assert not [
        r
        for r in _cover_share_rows(session, company.id, counted, storage)
        if r["accession_number"] == filing.accession_number
    ]

    session.bulk_insert_mappings(FinancialFact, rows)
    session.flush()
    summed = [c.shares for c in share_counts(session, company.id) if c.record_date == _COVER_DAY]
    assert summed == [Decimal(2500)]
    unsummed = share_counts(session, company.id, sum_classes=False)
    assert not [c for c in unsummed if c.record_date == _COVER_DAY]
    # Per-class rows never reach metric selection, which expects company totals.
    assert all(not f.dimensions for f in facts_repo.facts_for_company(session, company.id))
