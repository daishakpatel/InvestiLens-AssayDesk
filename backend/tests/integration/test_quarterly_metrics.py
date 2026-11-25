"""Phase 7b integration: persisted quarterly + TTM metrics, metric_gaps, and their API.

Ingests the NVDA/AAPL/JPM fixtures (mock SEC) and a synthetic company built to have a missing
YTD input, a three-quarter TTM window, unequal quarters and a share-count jump. Skips offline.
Refs: FR-QT-001 … FR-QT-012, DR-QT-001 … DR-QT-004, CIT-003.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.citation.resolver import resolve_source
from app.db import get_db
from app.finance.builder import build_company_metrics
from app.ingestion.sec import ingest_company
from app.main import app
from app.models import Company, FinancialFact, FinancialMetric, MetricGap
from app.providers.sec.mock import MockSecSource
from app.repositories import metrics as metrics_repo
from app.utils.storage import FilesystemObjectStorage

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


def _fixture_company(session: Session, ticker: str, tmp_path: Path) -> Company:
    ingest_company(session, ticker, sec=MockSecSource(), storage=FilesystemObjectStorage(tmp_path))
    company = session.scalars(select(Company).where(Company.ticker == ticker)).one()
    build_company_metrics(session, company)
    return company


def _rows(session: Session, company: Company, **where: object) -> list[FinancialMetric]:
    conditions = [FinancialMetric.company_id == company.id, FinancialMetric.is_latest.is_(True)]
    conditions += [getattr(FinancialMetric, k) == v for k, v in where.items()]
    return list(session.scalars(select(FinancialMetric).where(*conditions)))


def _gaps(session: Session, company: Company) -> set[tuple[str, str, str]]:
    return {
        (g.metric, g.period_label, g.reason)
        for g in session.scalars(select(MetricGap).where(MetricGap.company_id == company.id))
    }


@pytest.mark.parametrize("ticker", ["NVDA", "AAPL", "JPM"])
def test_operating_cash_flow_quarters_sum_to_the_annual_figure(
    session: Session, tmp_path: Path, ticker: str
) -> None:
    # DR-QT-004, FR-QT-002/006: Q1..Q4 persisted for at least the two latest complete fiscal
    # years, and they add up to the 10-K's FY value exactly (Decimal equality).
    company = _fixture_company(session, ticker, tmp_path)
    quarters = _rows(session, company, metric_name="operating_cash_flow", period_type="Q")
    annual = {
        r.fiscal_year: r.metric_value
        for r in _rows(session, company, metric_name="operating_cash_flow", period_type="FY")
    }
    by_year: dict[int, dict[int, Decimal]] = {}
    for row in quarters:
        assert row.fiscal_year is not None and row.fiscal_quarter is not None
        assert row.metric_value is not None
        by_year.setdefault(row.fiscal_year, {})[row.fiscal_quarter] = row.metric_value
    complete = sorted(fy for fy, q in by_year.items() if set(q) == {1, 2, 3, 4})
    assert len(complete) >= 2
    for fy in complete:
        assert sum(by_year[fy].values(), Decimal(0)) == annual[fy]

    # The regression: Q4 operating cash flow used to be silently missing.
    q4 = next(r for r in quarters if r.fiscal_quarter == 4 and r.fiscal_year == complete[-1])
    assert q4.is_derived and q4.source_id == (
        f"derived:{ticker}:operating_cash_flow:Q4-FY{complete[-1]}"
    )
    assert (q4.quality_flags or {})["derived"] == "Q4 = FY - 9M YTD"
    # FR-QT-007: no more than 12 quarters are kept.
    assert len({(r.fiscal_year, r.fiscal_quarter) for r in quarters}) <= 12


def test_derived_rows_cite_inputs_that_resolve(session: Session, tmp_path: Path) -> None:
    # CIT-003, FR-QT-003/007: every derived quarter has a formula and inputs, and each input
    # resolves through the citation resolver; so does every TTM input.
    company = _fixture_company(session, "NVDA", tmp_path)
    derived_quarters = _rows(session, company, period_type="Q", is_derived=True)
    assert derived_quarters
    for row in derived_quarters:
        flags = row.quality_flags or {}
        assert flags.get("derived") and flags.get("inputs"), row.source_id
        for item in flags["inputs"]:
            assert item["source_id"], (row.source_id, item)
            assert resolve_source(session, item["source_id"]) is not None, item
        assert resolve_source(session, row.source_id or "") is not None
    for row in _rows(session, company, period_type="TTM"):
        for item in (row.quality_flags or {}).get("inputs", []):
            if item.get("source_id"):
                assert resolve_source(session, item["source_id"]) is not None, (row.period, item)


def test_nvidia_quarters_are_labelled_by_fiscal_year(session: Session, tmp_path: Path) -> None:
    # FR-QT-005: NVIDIA's year ends in late January; a quarter ending in April is Q1 of the
    # next fiscal year, whatever the calendar says.
    company = _fixture_company(session, "NVDA", tmp_path)
    revenue = _rows(session, company, metric_name="revenue", period_type="Q")
    april = [r for r in revenue if r.period_end and r.period_end.month == 4]
    assert april and all(
        r.fiscal_quarter == 1 and r.period_end and r.fiscal_year == r.period_end.year + 1
        for r in april
    )
    january = [r for r in revenue if r.period_end and r.period_end.month == 1]
    assert january and all(
        r.fiscal_quarter == 4 and r.period_end and r.fiscal_year == r.period_end.year
        for r in january
    )
    assert all(r.period == f"Q{r.fiscal_quarter}-FY{r.fiscal_year}" for r in revenue)

    # NVIDIA's June 2024 10:1 split: the TTM EPS identity must not mix pre- and post-split
    # figures (it read -2.28 that way); every term is cited by the raw fact it used.
    eps = _rows(session, company, metric_name="eps_diluted", period_type="TTM")
    assert eps and all(r.metric_value is not None and r.metric_value > 0 for r in eps)
    ordered = sorted(eps, key=lambda r: (r.fiscal_year or 0, r.fiscal_quarter or 0))
    values = [r.metric_value or Decimal(0) for r in ordered]
    assert values == sorted(values)  # NVIDIA's trailing EPS rose every quarter in this window
    for row in eps:
        for item in (row.quality_flags or {})["inputs"]:
            assert item["source_id"].startswith("xbrl:")
            assert resolve_source(session, item["source_id"]) is not None

    # Valuation, portfolio and research read the latest *annual* figure: a quarter of the year
    # in progress has a later fiscal year and must not displace it.
    latest = metrics_repo.get_latest_metrics(
        session, company_id=company.id, metric_names=["revenue"]
    )
    assert latest and latest[0].period_type == "FY"


def _synthetic(session: Session) -> Company:
    """FY2025 (calendar) plus three quarters of FY2026, shaped to exercise every 7b rule.

    - revenue: quarters 100, 100, 100 then FY 1300 (Q4 = 1000), FY2026 quarters 100 each;
    - gross profit: 10, 10, 10, FY 930 (Q4 = 900): quarterly margins 10%, 10%, 10%, 90%;
    - operating cash flow: 3M and 6M of FY2025 but no 9M -> Q3/Q4 FY2025 missing;
    - diluted shares 100 -> 103 -> 110 across FY2026's quarters (+10%);
    - EPS: FY2025 13.00, 9M FY2025 3.00, 9M FY2026 3.30 -> TTM 13.30 by the YTD identity.
    """
    company = Company(ticker="QTR", cik="0000000077", name="Quarter Co", sector="Technology")
    session.add(company)
    session.flush()
    y25, y26 = date(2025, 1, 1), date(2026, 1, 1)
    q_ends_25 = [date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30)]
    q_ends_26 = [date(2026, 3, 31), date(2026, 6, 30), date(2026, 9, 30)]
    q_starts_25 = [y25, date(2025, 4, 1), date(2025, 7, 1)]
    q_starts_26 = [y26, date(2026, 4, 1), date(2026, 7, 1)]
    facts: list[tuple[str, date, date, str, str]] = [
        ("us-gaap:Revenues", y25, date(2025, 12, 31), "1300", "USD"),
        ("us-gaap:GrossProfit", y25, date(2025, 12, 31), "930", "USD"),
        (
            "us-gaap:NetCashProvidedByUsedInOperatingActivities",
            y25,
            date(2025, 12, 31),
            "400",
            "USD",
        ),
        ("us-gaap:NetCashProvidedByUsedInOperatingActivities", y25, q_ends_25[0], "50", "USD"),
        ("us-gaap:NetCashProvidedByUsedInOperatingActivities", y25, q_ends_25[1], "120", "USD"),
        ("us-gaap:NetCashProvidedByUsedInOperatingActivities", y26, q_ends_26[0], "60", "USD"),
        ("us-gaap:NetCashProvidedByUsedInOperatingActivities", y26, q_ends_26[1], "130", "USD"),
        ("us-gaap:NetCashProvidedByUsedInOperatingActivities", y26, q_ends_26[2], "210", "USD"),
        ("us-gaap:EarningsPerShareDiluted", y25, date(2025, 12, 31), "13.00", "USD/shares"),
        ("us-gaap:EarningsPerShareDiluted", y25, q_ends_25[2], "3.00", "USD/shares"),
        ("us-gaap:EarningsPerShareDiluted", y26, q_ends_26[2], "3.30", "USD/shares"),
    ]
    for starts, ends in ((q_starts_25, q_ends_25), (q_starts_26, q_ends_26)):
        for start, end in zip(starts, ends, strict=True):
            facts.append(("us-gaap:Revenues", start, end, "100", "USD"))
            facts.append(("us-gaap:GrossProfit", start, end, "10", "USD"))
    for start, end, shares in zip(q_starts_26, q_ends_26, ("100", "103", "110"), strict=True):
        facts.append(
            (
                "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
                start,
                end,
                shares,
                "shares",
            )
        )
    session.add_all(
        FinancialFact(
            company_id=company.id,
            accession_number=f"qtr-{end.isoformat()}",
            concept_tag=tag,
            period_start=start,
            period_end=end,
            period_type="duration",
            value=Decimal(value),
            unit=unit,
            filed_date=date(2026, 10, 1),
            is_amended=False,
        )
        for tag, start, end, value, unit in facts
    )
    session.flush()
    return company


def test_gaps_ttm_windows_and_quality_flags(session: Session) -> None:
    company = _synthetic(session)
    counts = build_company_metrics(session, company)
    assert counts.quarterly_metrics > 0 and counts.ttm_metrics > 0 and counts.metric_gaps > 0
    gaps = _gaps(session, company)

    # FR-QT-004: a missing YTD input yields no quarter and a gap with its reason.
    assert ("operating_cash_flow", "Q3-FY2025", "missing_ytd_3q") in gaps
    assert not _rows(session, company, metric_name="operating_cash_flow", period="Q3-FY2025")

    # DR-QT-002: three of four quarters -> no TTM row, and a gap; never an annualized figure.
    assert not _rows(session, company, metric_name="operating_cash_flow", period_type="TTM")
    assert ("operating_cash_flow", "TTM-Q3-FY2026", "incomplete_window") in gaps

    ttm = {r.metric_name: r for r in _rows(session, company, period="TTM-Q3-FY2026")}
    assert ttm["revenue"].metric_value == Decimal(1300)
    assert ttm["revenue"].period_type == "TTM" and ttm["revenue"].period_end == date(2026, 9, 30)
    # DR-QT-003: TTM margin = TTM gross profit / TTM revenue (71.5%), not the 30% average.
    assert ttm["gross_margin"].metric_value == (Decimal(930) / Decimal(1300)).quantize(
        Decimal("0.00000001")
    )
    assert (ttm["gross_margin"].quality_flags or {})["inputs"] == [
        {"name": "gross_profit", "source_id": "derived:QTR:gross_profit:TTM-Q3-FY2026"},
        {"name": "revenue", "source_id": "derived:QTR:revenue:TTM-Q3-FY2026"},
    ]
    # FR-QT-008/009: EPS by the YTD identity, flagged for the +10% diluted share count.
    eps = ttm["eps_diluted"]
    assert eps.metric_value == Decimal("13.30") and eps.unit == "USD/shares"
    flags = eps.quality_flags or {}
    assert flags["quality_flag"] == "share_count_changed"
    assert "share_count_changed" in flags["warnings"]
    assert flags["derived"] == "TTM = FY2025 - 9M YTD FY2025 + 9M YTD FY2026"


def test_rebuild_refreshes_gaps_idempotently(session: Session) -> None:
    # FR-QT-010, ING-001: gaps are rebuilt, not accumulated.
    company = _synthetic(session)
    build_company_metrics(session, company)
    first = _gaps(session, company)
    build_company_metrics(session, company)
    assert _gaps(session, company) == first
    count = session.scalar(
        select(func.count()).select_from(MetricGap).where(MetricGap.company_id == company.id)
    )
    assert count == len(first)


@pytest.fixture
def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(session, "commit", session.flush)
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_coverage_tells_not_applicable_from_missing(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    # FR-QT-010: per metric, which quarters exist and which are missing and why; a bank's
    # gross margin is N/A for the sector (DR-042), not a data gap.
    _fixture_company(session, "JPM", tmp_path)
    resp = client.get(f"{_P}/companies/JPM/metrics/coverage")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["quarters"]) == 12 and body["quarters"][-1] == "Q2-FY2026"
    by_name = {m["metric_name"]: m for m in body["metrics"]}
    for na in ("gross_margin", "gross_profit", "roic"):
        assert by_name[na]["applicable"] is False and by_name[na]["missing"] == []
    ocf = by_name["operating_cash_flow"]
    assert ocf["applicable"] and "Q4-FY2025" in ocf["present"] and "TTM-Q2-FY2026" in ocf["present"]
    debt = by_name["long_term_debt"]  # JPM stopped tagging it in 2014: missing, not N/A
    assert debt["applicable"] and {g["reason"] for g in debt["missing"]} == {"missing_balance"}
    eps = by_name["eps_diluted"]
    assert {"period": "Q4-FY2025", "reason": "not_additive"} in eps["missing"]
    assert client.get(f"{_P}/companies/ZZZZ/metrics/coverage").status_code == 404


def test_financials_quarterly_and_ttm_params(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    # FR-QT-011/012: ?period_type=Q|TTM|FY and ?limit= (default 8, max 20); points carry the
    # source ID and, for derived rows, the formula.
    _fixture_company(session, "AAPL", tmp_path)
    url = f"{_P}/companies/AAPL/financials"
    quarterly = client.get(url, params={"metrics": "operating_cash_flow", "period_type": "Q"})
    assert quarterly.status_code == 200
    points = quarterly.json()["series"][0]["points"]
    assert len(points) == 8 and points[-1]["period"] == "Q3-FY2026"
    q4 = next(p for p in points if p["period"] == "Q4-FY2025")
    assert q4["source_id"] == "derived:AAPL:operating_cash_flow:Q4-FY2025"
    assert q4["formula"] == "Q4 = FY - 9M YTD"
    q1 = next(p for p in points if p["period"] == "Q1-FY2026")
    assert q1["source_id"] == "xbrl:AAPL:operating_cash_flow:Q1-FY2026" and q1["formula"] is None

    ttm = client.get(url, params={"metrics": "revenue", "period_type": "TTM", "limit": 3})
    ttm_points = ttm.json()["series"][0]["points"]
    assert [p["period"] for p in ttm_points] == ["TTM-Q1-FY2026", "TTM-Q2-FY2026", "TTM-Q3-FY2026"]
    assert ttm_points[-1]["formula"].startswith("TTM = Q4-FY2025 + Q1-FY2026")

    annual = client.get(url, params={"metrics": "revenue"}).json()["series"][0]["points"]
    assert len(annual) > 8  # FY keeps every year unless a limit is given
    assert client.get(url, params={"period_type": "Q", "limit": 21}).status_code == 422
    assert client.get(url, params={"period_type": "WEEK"}).status_code == 422


@pytest.mark.parametrize("ticker", ["NVDA", "JPM"])
def test_quarter_margins_ttm_returns_and_annual_roic_cagr(
    session: Session, tmp_path: Path, ticker: str
) -> None:
    """§10.3/§37.7 (ADR-0073): quarterly margins, TTM net margin/ROE/ROA, annual ROIC and 3-year
    revenue CAGR are stored; a Q4 trailing window equals its fiscal year; a bank's are N/A."""
    from app.finance.ratio_builder import build_company_ratios

    company = _fixture_company(session, ticker, tmp_path)
    build_company_ratios(session, company)

    def value(metric: str, period: str) -> Decimal | None:
        rows = _rows(session, company, metric_name=metric, period=period)
        assert rows, (metric, period)
        return rows[0].metric_value

    quarters = _rows(session, company, metric_name="net_margin", period_type="Q")
    assert quarters and all(r.metric_value is not None for r in quarters)
    fy = max(r.fiscal_year for r in quarters if r.fiscal_quarter == 4 and r.fiscal_year)
    for metric in ("net_margin", "roe", "roa"):
        assert value(metric, f"TTM-Q4-FY{fy}") == value(metric, f"FY{fy}"), metric

    gross = _rows(session, company, metric_name="gross_margin", period_type="Q")
    roic = _rows(session, company, metric_name="roic", period_type="FY")
    cagr = _rows(session, company, metric_name="revenue_cagr_3y", period_type="FY")
    assert cagr and all(r.metric_value is not None for r in cagr)
    assert min(r.fiscal_year or 0 for r in cagr) >= min(r.fiscal_year or 0 for r in roic) + 3
    if ticker == "JPM":  # DR-042: a bank has no gross margin or invested capital, never a number
        assert gross and all(r.metric_value is None for r in gross)
        assert roic and all(r.metric_value is None for r in roic)
    else:
        assert gross and all(r.metric_value is not None for r in gross)
        latest = next(r for r in roic if r.fiscal_year == fy)
        assert latest.metric_value is not None
        assert resolve_source(session, latest.source_id or "") is not None
