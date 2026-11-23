"""Earnings-quality persistence and endpoint (Phase 7d). Real Postgres, mock SEC and prices.

Requirement IDs: FR-EQ-006/008/009/010, DR-EQ-001/002.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.api.earnings_quality import to_point
from app.citation.resolver import resolve_source
from app.db import get_db
from app.finance.beneish import BeneishYear
from app.finance.builder import build_company_metrics
from app.finance.quality_builder import QUALITY_METRICS, build_company_earnings_quality
from app.finance.ratio_builder import build_company_ratios
from app.ingestion.prices import ingest_prices
from app.ingestion.sec import ingest_company
from app.main import app
from app.models import Company, FinancialMetric
from app.providers.prices.mock import MockPriceSource
from app.providers.sec.mock import MockSecSource
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


@pytest.fixture
def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(session, "commit", session.flush)
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _company(session: Session, ticker: str, tmp_path: Path) -> Company:
    """The production order: metrics, ratios, prices (which build valuation), then quality."""
    ingest_company(session, ticker, sec=MockSecSource(), storage=FilesystemObjectStorage(tmp_path))
    company = session.scalars(select(Company).where(Company.ticker == ticker)).one()
    build_company_metrics(session, company)
    build_company_ratios(session, company)
    ingest_prices(session, company, source=MockPriceSource())
    build_company_earnings_quality(session, company)
    return company


def _latest(session: Session, company: Company, name: str) -> FinancialMetric:
    year = session.scalar(
        select(func.max(FinancialMetric.fiscal_year)).where(
            FinancialMetric.company_id == company.id,
            FinancialMetric.metric_name == name,
            FinancialMetric.period_type == "FY",
            FinancialMetric.metric_value.is_not(None),
        )
    )
    return session.scalars(
        select(FinancialMetric).where(
            FinancialMetric.company_id == company.id,
            FinancialMetric.metric_name == name,
            FinancialMetric.period_type == "FY",
            FinancialMetric.fiscal_year == year,
        )
    ).one()


def test_measures_persist_with_resolvable_inputs(session: Session, tmp_path: Path) -> None:
    """FR-EQ-008: every measure is a derived FY row whose every input resolves (CIT-003)."""
    nvda = _company(session, "NVDA", tmp_path)
    for name in QUALITY_METRICS:
        row = _latest(session, nvda, name)
        assert row.metric_value is not None, f"NVDA {name} not computed"
        assert row.source_id == f"derived:NVDA:{name}:FY{row.fiscal_year}"
        assert resolve_source(session, row.source_id) is not None
        inputs = (row.quality_flags or {})["inputs"]
        assert inputs, name
        for item in inputs:
            if item["source_id"]:
                assert resolve_source(session, item["source_id"]) is not None, item


def test_altman_variant_band_and_split_adjusted_dilution(session: Session, tmp_path: Path) -> None:
    """FR-EQ-006: the model variant is stored. FR-EQ-008: dilution uses the 10-K's restated
    prior-year count, so NVIDIA's 10:1 split is not dilution."""
    nvda = _company(session, "NVDA", tmp_path)
    altman = _latest(session, nvda, "altman_z")
    flags = altman.quality_flags or {}
    assert flags["model_variant"] == "original" and "3674" in flags["variant_basis"]
    assert flags["band"] in {"distress", "grey", "safe"}
    split_year = session.scalars(
        select(FinancialMetric).where(
            FinancialMetric.company_id == nvda.id,
            FinancialMetric.metric_name == "dilution",
            FinancialMetric.period == "FY2025",  # Feb 2024 - Jan 2025 spans the June 2024 split
        )
    ).one()
    flags = split_year.quality_flags or {}
    assert flags["prior_shares_basis"] == "restated in the FY2025 10-K"
    assert split_year.metric_value is not None and abs(split_year.metric_value) < Decimal("0.05")
    prior = next(i for i in flags["inputs"] if i["name"] == "prior_shares")
    assert resolve_source(session, prior["source_id"]) is not None  # the raw restated fact


def test_endpoint_returns_five_years_with_status(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    """FR-EQ-009: five fiscal years per measure for NVDA and AAPL, each with status and inputs."""
    for ticker in ("NVDA", "AAPL"):
        _company(session, ticker, tmp_path)
        body = client.get(f"/api/v1/companies/{ticker}/earnings-quality").json()
        assert len(body["fiscal_years"]) == 5, ticker
        names = [m["metric_name"] for m in body["measures"]]
        assert names == list(QUALITY_METRICS)
        for measure in body["measures"]:
            assert len(measure["points"]) == 5, (ticker, measure["metric_name"])
            assert measure["explanation"]
            latest = measure["points"][-1]
            assert latest["status"] == "ok", (ticker, measure["metric_name"], latest)
            assert latest["source_id"] and latest["inputs"]
        beneish = next(m for m in body["measures"] if m["metric_name"] == "beneish_m")
        assert beneish["caveat"].startswith("Screening heuristic")
        assert beneish["points"][-1]["band"] in {"below_threshold", "above_threshold"}
        assert set(beneish["points"][-1]["details"]["indices"]) == {
            "dsri", "gmi", "aqi", "sgi", "depi", "sgai", "tata", "lvgi"
        }  # fmt: skip
        piotroski = next(m for m in body["measures"] if m["metric_name"] == "piotroski_f")
        assert len(piotroski["points"][-1]["details"]["criteria"]) == 9
        fcf = next(m for m in body["measures"] if m["metric_name"] == "fcf_conversion")
        assert fcf["latest_ttm"]["period"].startswith("TTM-")


def test_bank_scores_are_not_applicable_with_a_reason(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    """DR-EQ-001: JPM shows not_applicable for Beneish and Altman, with a reason string."""
    _company(session, "JPM", tmp_path)
    body = client.get("/api/v1/companies/JPM/earnings-quality").json()
    for name in ("beneish_m", "altman_z", "piotroski_f", "dso"):
        point = next(m for m in body["measures"] if m["metric_name"] == name)["points"][-1]
        assert point["status"] == "not_applicable" and point["value"] is None
        assert point["reason_text"].startswith("Not computed for banks")
        assert point["band"] is None


def test_insufficient_data_names_the_missing_input(session: Session, tmp_path: Path) -> None:
    """DR-EQ-002: the first stored year lacks inputs (at least its predecessor's), named."""
    nvda = _company(session, "NVDA", tmp_path)
    first = session.scalars(
        select(FinancialMetric)
        .where(
            FinancialMetric.company_id == nvda.id,
            FinancialMetric.metric_name == "beneish_m",
            FinancialMetric.period_type == "FY",
        )
        .order_by(FinancialMetric.fiscal_year)
    ).first()
    assert first is not None and first.metric_value is None
    point = to_point(first, "")
    assert point.status == "insufficient_data" and point.reason == "MISSING_INPUT"
    fields = list(BeneishYear.__dataclass_fields__)
    assert any(name.startswith("prior_") for name in point.missing)
    assert set(point.missing) <= {*fields, *(f"prior_{f}" for f in fields)}


def test_peer_percentile_hidden_below_three_peers(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    """FR-EQ-010: with only two other companies held, no percentile is shown, and why is said."""
    for ticker in ("NVDA", "AAPL", "JPM"):
        _company(session, ticker, tmp_path)
    body = client.get("/api/v1/companies/NVDA/earnings-quality").json()
    assert all(m["peer_percentile"] is None for m in body["measures"])
    assert body["notes"] and "Peer percentiles are hidden" in body["notes"][0]
    assert client.get("/api/v1/companies/NVDA/earnings-quality?years=0").status_code == 422
    assert client.get("/api/v1/companies/ZZZZ/earnings-quality").status_code == 404


def test_peer_percentile_compares_one_calendar_year(session: Session) -> None:
    """§37.1, ADR-0070: a January fiscal year sits beside December peers' prior fiscal year; a
    peer's newer year is never used; a September company not yet matched falls back one year."""
    from app.api.earnings_quality import _peer_context
    from app.finance.metrics import percentile_rank

    def company(ticker: str, fye: str, values: dict[int, str]) -> Company:
        c = Company(
            ticker=ticker, cik=f"9{len(ticker)}{ticker}"[:10], name=ticker, fiscal_year_end=fye
        )
        session.add(c)
        session.flush()
        for fy, value in values.items():
            session.add(
                FinancialMetric(
                    company_id=c.id,
                    period=f"FY{fy}",
                    fiscal_year=fy,
                    period_type="FY",
                    metric_name="dso",
                    metric_value=Decimal(value),
                    unit="days",
                    is_derived=True,
                    source_id=f"derived:{ticker}:dso:FY{fy}",
                    is_latest=True,
                )
            )
        session.flush()
        return c

    peers = [
        company("DECA", "12-31", {2024: "100", 2025: "10", 2026: "1000"}),  # 2026: a newer year
        company("DECB", "12-31", {2024: "200", 2025: "20"}),
        company("DECC", "12-31", {2024: "300", 2025: "30"}),
    ]
    ids = [p.id for p in peers]
    january = company("JANX", "01-26", {2025: "5", 2026: "25"})  # FY2026 = calendar 2025

    def context(target: Company) -> tuple[Decimal | None, int, int | None]:
        rows = list(
            session.scalars(select(FinancialMetric).where(FinancialMetric.company_id == target.id))
        )
        return _peer_context(session, target, rows, ids)["dso"]

    expected = percentile_rank(Decimal("25"), [Decimal(v) for v in ("25", "10", "20", "30")]).value
    assert context(january) == (expected, 3, 2025)

    # FY2026 to September 2026 is calendar 2026, which only one peer has: compare calendar 2025.
    september = company("SEPX", "09-27", {2025: "15", 2026: "40"})
    expected = percentile_rank(Decimal("15"), [Decimal(v) for v in ("15", "10", "20", "30")]).value
    assert context(september) == (expected, 3, 2025)
