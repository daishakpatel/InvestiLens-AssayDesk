"""Persist each company's valuation multiples over time (Phase 7h, FR-PS-008, ADR-0046).

For the last five fiscal year ends and the latest close, every multiple in
`point_in_time.MULTIPLES` is computed from what was known on that date: the close on or before
it, the latest share count reported (and filed) by then, and TTM figures from filings made by
then (DR-PS-003). Rows replace the company's previous history on every run (idempotent), and each
records its price, share count and every figure's source IDs (FR-PS-008).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.finance.concept_map import load_concept_map
from app.finance.facts import fact_view_from_orm, with_composites
from app.finance.point_in_time import figures_as_of, inputs_record, multiples
from app.finance.valuation_builder import (
    PricePoint,
    count_as_of,
    load_splits,
    price_on_or_before,
    share_counts,
    shares_on,
)
from app.models import Company, FinancialMetric, PriceHistory, ValuationHistory
from app.repositories import facts as facts_repo

HISTORY_YEARS = 5
FISCAL_YEAR_END = "fiscal_year_end"
CURRENT = "current"


def year_ends(
    session: Session, company_id: int, years: int = HISTORY_YEARS
) -> list[tuple[int, date]]:
    """The last `years` fiscal year ends, oldest first, from the annual rows' period ends."""
    rows = session.execute(
        select(FinancialMetric.fiscal_year, FinancialMetric.period_end).where(
            FinancialMetric.company_id == company_id,
            FinancialMetric.period_type == "FY",
            FinancialMetric.is_derived.is_(False),
            FinancialMetric.period_end.is_not(None),
        )
    )
    ends: dict[int, date] = {}
    for fy, end in rows:
        if fy is not None and end is not None:
            ends[fy] = max(end, ends.get(fy, end))
    return sorted(ends.items())[-years:]


def build_valuation_history(session: Session, company: Company) -> int:
    """Rebuild a company's valuation history. Returns the rows written."""
    session.execute(delete(ValuationHistory).where(ValuationHistory.company_id == company.id))
    prices = [
        PricePoint(day, close)
        for day, close in session.execute(
            select(PriceHistory.date, PriceHistory.close)
            .where(PriceHistory.company_id == company.id)
            .order_by(PriceHistory.date)
        ).all()
        if close is not None
    ]
    if not prices:
        return 0
    cmap = load_concept_map(ticker=company.ticker, cik=company.cik, sector=company.sector)
    facts = with_composites(
        [fact_view_from_orm(f) for f in facts_repo.facts_for_company(session, company.id)],
        cmap.composites,
    )
    counts = share_counts(session, company.id, sum_classes=cmap.sum_share_classes)
    splits = load_splits(session, company.id)
    dates: list[tuple[int | None, date, str]] = [
        (fy, end, FISCAL_YEAR_END) for fy, end in year_ends(session, company.id)
    ]
    dates.append((None, prices[-1].day, CURRENT))

    rows: list[dict[str, Any]] = []
    for fiscal_year, as_of, kind in dates:
        price = price_on_or_before(prices, as_of)
        count = count_as_of(counts, as_of)
        shares = shares_on(count, price.day, splits) if count and price else None
        quarter_end, figures = figures_as_of(facts, cmap, as_of)
        extra: dict[str, Any] = {
            "price": None
            if price is None
            else {
                "date": price.day.isoformat(),
                "close": str(price.close),
                "source_id": f"price:{company.ticker}:{price.day.isoformat()}",
            },
            "shares": None
            if count is None or shares is None
            else {
                "value": str(shares),
                "record_date": count.record_date.isoformat(),
                "source_id": count.source_id,
            },
        }
        for multiple in multiples(
            price=price.close if price else None,
            shares=shares,
            figures=figures,
            ev_applies=cmap.applies("ev_ebitda"),
            book_applies=cmap.applies("pb_ratio"),
        ):
            rows.append(
                {
                    "company_id": company.id,
                    "as_of_date": as_of,
                    "kind": kind,
                    "fiscal_year": fiscal_year,
                    "multiple": multiple.name,
                    "value": multiple.value,
                    "status": multiple.status,
                    "inputs": inputs_record(
                        quarter_end,
                        figures,
                        extra | {"unit": multiple.unit, "warnings": multiple.result.warnings},
                    ),
                }
            )
    session.execute(insert(ValuationHistory), rows)
    return len(rows)
