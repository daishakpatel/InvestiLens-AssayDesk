"""Company, financials, prices, and ownership response schemas (spec §24.2)."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.common import Freshness
from app.schemas.sources import MetricResult


class Company(BaseModel):
    ticker: str
    cik: str
    name: str
    exchange: str | None = None
    sector: str | None = None
    industry: str | None = None
    fiscal_year_end: str | None = None


class CompanySearchResult(BaseModel):
    """A single fuzzy-search hit (FR-001)."""

    ticker: str
    cik: str
    name: str
    exchange: str | None = None
    score: float = Field(ge=0, le=1)


class CompanyResponse(BaseModel):
    company: Company
    freshness: Freshness


class MetricSeriesPoint(BaseModel):
    period: str  # "FY2025", "Q3-FY2026", "TTM-Q3-FY2026" (fiscal, separate from calendar dates)
    period_end: str | None = None  # ISO-8601 calendar date
    value: Decimal | None
    unit: str
    source_id: str | None = None  # backend-issued; resolves via GET /sources/{source_id}
    formula: str | None = None  # derived rows only, e.g. "Q3 = 9M YTD - 6M YTD" (CIT-003)


class MetricSeries(BaseModel):
    metric_name: str
    points: list[MetricSeriesPoint]
    applicable: bool = True  # false: N/A for the company's sector (DR-042), not a missing value


class FinancialsResponse(BaseModel):
    ticker: str
    period_type: str  # FY|Q|TTM
    series: list[MetricSeries]
    freshness: Freshness


class MetricGapItem(BaseModel):
    period: str  # "Q2-FY2026" or "TTM-Q2-FY2026"
    reason: str  # missing_ytd_2q, missing_fy, incomplete_window, NEGATIVE_BASE, ...


class MetricCoverage(BaseModel):
    """Which quarterly / TTM periods of one metric exist, and why the others do not (FR-QT-010)."""

    metric_name: str
    # False = not applicable to the company's sector (DR-042): render N/A, never as missing data.
    applicable: bool
    present: list[str]  # periods with a stored value, oldest first
    missing: list[MetricGapItem]


class MetricsCoverageResponse(BaseModel):
    ticker: str
    quarters: list[str]  # the stored quarter window (up to 12), oldest first
    metrics: list[MetricCoverage]
    freshness: Freshness


class ValuationResponse(BaseModel):
    ticker: str
    metrics: list[MetricResult]
    freshness: Freshness


class PricePoint(BaseModel):
    date: str  # ISO-8601 date
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    adj_close: Decimal | None = None
    volume: int | None = None


class PricesResponse(BaseModel):
    ticker: str
    unit: str = "USD"
    interval: str = "1d"
    points: list[PricePoint]
    freshness: Freshness


class InsiderTransaction(BaseModel):
    insider_name: str
    role: str | None = None
    transaction_date: str | None = None
    code: str | None = None
    shares: int | None = None
    price: Decimal | None = None


class OwnershipHolding(BaseModel):
    filer_name: str
    period_end: str | None = None
    shares: int | None = None
    value: Decimal | None = None
    change_shares: int | None = None
