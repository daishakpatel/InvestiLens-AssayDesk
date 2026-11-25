"""Persist derived (non-price) ratios into `financial_metrics` (Phase 1c).

Reads the canonical annual base metrics Phase 1b wrote (revenue, net_income, …), runs the pure
formulas in `metrics.py` over them, and upserts the results as `is_derived = True` rows with a
`formula_id` and lineage to the source facts (DR-040/043). Price-dependent valuation metrics
(P/E, market cap, EV) are deferred until Phase 1d supplies prices. Sector-inapplicable metrics
are persisted with value NULL and a reason code, never zero (DR-042).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.finance import metrics
from app.finance.concept_map import ConceptMap, load_concept_map
from app.models import Company, FinancialMetric
from app.repositories import metrics as metrics_repo
from app.schemas.sources import MetricResult

_DERIVED_ACCESSION = "derived"  # non-null sentinel so the upsert key is idempotent
_CAGR_YEARS = 3


class Canonical:
    """Canonical annual base values for one company: (fiscal_year, metric) -> value/source."""

    def __init__(self, rows: list[FinancialMetric]) -> None:
        self._value: dict[tuple[int, str], Decimal] = {}
        self._source: dict[tuple[int, str], str] = {}
        for r in rows:
            if r.fiscal_year is None:
                continue
            key = (r.fiscal_year, r.metric_name)
            if r.metric_value is not None:
                self._value[key] = r.metric_value
            if r.source_id:
                self._source[key] = r.source_id

    def value(self, fiscal_year: int, metric: str) -> Decimal | None:
        return self._value.get((fiscal_year, metric))

    def source(self, fiscal_year: int, metric: str) -> str | None:
        return self._source.get((fiscal_year, metric))

    @property
    def fiscal_years(self) -> list[int]:
        return sorted({fy for fy, _ in self._value})


def persist_derived(
    session: Session,
    company_id: int,
    ticker: str,
    fiscal_year: int,
    result: MetricResult,
    *,
    name: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Upsert one derived FY row. `name` overrides the stored metric name (default: the formula
    id); `extra` adds keys to `quality_flags` beside the lineage (e.g. a score's band)."""
    metric_name = name or result.formula_id
    lineage = [
        {"name": i.name, "source_id": i.source_id}
        | ({"value": str(i.value)} if i.value is not None else {})
        for i in result.inputs
    ]
    metrics_repo.upsert_metric(
        session,
        company_id=company_id,
        period=f"FY{fiscal_year}",
        fiscal_year=fiscal_year,
        fiscal_quarter=None,
        period_start=None,
        period_end=None,
        period_type="FY",
        metric_name=metric_name,
        metric_value=result.value,  # None = N/A or uncomputable
        unit=result.unit,
        is_derived=True,
        formula_id=result.formula_id,
        # Company-scoped so a derived id is globally unique (two companies' gross_margin for the
        # same fiscal year must not collide — this breaks cross-company comparison evidence and
        # made GET /sources ambiguous; comparison, §37.1, surfaced it).
        source_id=f"derived:{ticker}:{metric_name}:FY{fiscal_year}",
        as_reported_accession=_DERIVED_ACCESSION,
        is_latest=True,
        quality_flags={
            "inputs": lineage,
            "formula_version": result.formula_version,
            "warnings": result.warnings,
            **(extra or {}),
        },
    )


def _compute_year(
    canonical: Canonical, cmap: ConceptMap, fy: int, prior: int | None, ticker: str = ""
) -> list[MetricResult]:
    def v(metric: str, year: int | None = None) -> Decimal | None:
        return canonical.value(year if year is not None else fy, metric)

    def src(*names: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in names:
            s = canonical.source(fy, name)
            if s:
                out[name] = s
        return out

    debt = metrics.total_debt(
        v("short_term_debt"), v("long_term_debt"), sources=src("short_term_debt", "long_term_debt")
    )
    results = [
        metrics.gross_margin(
            v("gross_profit"),
            v("revenue"),
            v("cost_of_revenue"),
            applicable=cmap.applies("gross_margin"),
            sources=src("gross_profit", "revenue", "cost_of_revenue"),
        ),
        metrics.operating_margin(
            v("operating_income"), v("revenue"), sources=src("operating_income", "revenue")
        ),
        metrics.net_margin(v("net_income"), v("revenue"), sources=src("net_income", "revenue")),
        metrics.ebitda(
            v("operating_income"),
            v("depreciation_amortization"),
            sources=src("operating_income", "depreciation_amortization"),
        ),
        metrics.free_cash_flow(
            v("operating_cash_flow"), v("capex"), sources=src("operating_cash_flow", "capex")
        ),
        metrics.debt_to_equity(
            debt.value, v("shareholders_equity"), sources=src("shareholders_equity")
        ),
        metrics.current_ratio(
            v("current_assets"),
            v("current_liabilities"),
            applicable=cmap.applies("current_ratio"),
            sources=src("current_assets", "current_liabilities"),
        ),
        metrics.roe(
            v("net_income"),
            v("shareholders_equity"),
            sources=src("net_income", "shareholders_equity"),
        ),
        metrics.roa(v("net_income"), v("total_assets"), sources=src("net_income", "total_assets")),
        metrics.sbc_pct_revenue(
            v("stock_based_compensation"),
            v("revenue"),
            sources=src("stock_based_compensation", "revenue"),
        ),
        metrics.capex_intensity(v("capex"), v("revenue"), sources=src("capex", "revenue")),
        debt,
    ]
    # Annual ROIC beside the stored TTM one (§37.7): the fiscal year's own tax rate and balances.
    tax = metrics.effective_tax_rate(
        v("income_tax_expense"),
        v("pretax_income"),
        sources=src("income_tax_expense", "pretax_income"),
    )
    derived = {
        "tax_rate": f"derived:{ticker}:effective_tax_rate:FY{fy}",
        "total_debt": f"derived:{ticker}:total_debt:FY{fy}",
    }
    results += [
        tax,
        metrics.roic(
            v("operating_income"),
            tax.value,
            debt.value,
            v("shareholders_equity"),
            v("cash_and_equivalents"),
            applicable=cmap.applies("roic"),
            sources={
                **src("operating_income", "shareholders_equity", "cash_and_equivalents"),
                **derived,
            },
        ),
    ]
    fcf = next(r for r in results if r.formula_id == "free_cash_flow")
    results.append(metrics.fcf_margin(fcf.value, v("revenue"), sources=src("revenue")))
    if prior is not None:
        results.append(
            metrics.yoy_growth(v("revenue"), v("revenue", prior), sources=src("revenue"))
        )
    return results


def build_company_ratios(session: Session, company: Company) -> int:
    """Compute and persist derived ratios for a company. Returns the number of rows written."""
    cmap = load_concept_map(ticker=company.ticker, cik=company.cik, sector=company.sector)
    rows = list(
        session.scalars(
            select(FinancialMetric).where(
                FinancialMetric.company_id == company.id,
                FinancialMetric.period_type == "FY",
                FinancialMetric.is_derived.is_(False),
                FinancialMetric.is_latest.is_(True),
            )
        )
    )
    canonical = Canonical(rows)
    years = canonical.fiscal_years
    written = 0
    for index, fy in enumerate(years):
        prior = years[index - 1] if index > 0 else None
        for result in _compute_year(canonical, cmap, fy, prior, company.ticker):
            persist_derived(session, company.id, company.ticker, fy, result)
            written += 1
        start = fy - _CAGR_YEARS  # spec §10.2 "3-Year CAGR", stored only once the start year exists
        if canonical.value(start, "revenue") is not None:
            cagr = metrics.cagr(
                canonical.value(start, "revenue"),
                canonical.value(fy, "revenue"),
                _CAGR_YEARS,
                sources={
                    k: s
                    for k, s in (
                        ("start", canonical.source(start, "revenue")),
                        ("end", canonical.source(fy, "revenue")),
                    )
                    if s
                },
            )
            persist_derived(session, company.id, company.ticker, fy, cagr, name="revenue_cagr_3y")
            written += 1
    return written
