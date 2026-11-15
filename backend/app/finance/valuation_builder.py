"""Persist price-dependent valuation metrics into `financial_metrics` (§10.7, §12, FR-024).

Two kinds of row per company, both `is_derived` with lineage, like the ratios:

- `FY<year>`: the multiple at that fiscal year's end, from the raw close on the last trading day on
  or before the year end, the cover-page share count reported nearest after it, and that year's
  figures. These feed comparisons, multi-year history and the report's valuation section.
- the latest close (`period_type` "LATEST", period = the price date): the same multiples against
  the latest cover-page share count and the latest fiscal year's figures. This is what the
  company header shows. It is rebuilt whenever prices are ingested.

P/E uses the raw close with diluted EPS, and the share count is moved to the price date across
any stock split in between (DR-025/026). Every row stores its price date, close, share count,
share record date and fiscal year, so it can be reproduced from stored data (FR-024).

Since Phase 7h (ADR-0046) the latest-close row uses TTM figures as known at the price date
(`point_in_time`): the same numbers as the valuation bands' "today", with P/E as market cap / TTM
net income. Fiscal-year rows keep that year's figures, labelled as such in their basis.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.finance import metrics
from app.finance.concept_map import load_concept_map
from app.finance.facts import fact_view_from_orm, with_composites
from app.finance.point_in_time import derived_values, figures_as_of
from app.finance.shares import total_shares_outstanding
from app.models import Company, CorporateAction, FinancialFact, FinancialMetric, PriceHistory
from app.repositories import facts as facts_repo
from app.repositories import metrics as metrics_repo
from app.schemas.sources import MetricResult

SHARES_CONCEPT = "dei:EntityCommonStockSharesOutstanding"
LATEST = "LATEST"
_DERIVED_ACCESSION = "derived"  # non-null sentinel so the upsert key is idempotent (as ratios)
# Stored names follow the metric catalog (frontend + valuation endpoint), not the formula ids.
_NAMES = {"ev_to_revenue": "ev_revenue", "ev_to_ebitda": "ev_ebitda"}
VALUATION_METRICS = (
    "market_cap",
    "enterprise_value",
    "pe_ratio",
    "ps_ratio",
    "p_fcf",
    "ev_revenue",
    "ev_ebitda",
    "fcf_yield",
)


@dataclass(frozen=True)
class ShareCount:
    record_date: date
    shares: Decimal
    source_id: str
    filed: date | None = None  # when the filing reporting it was filed (point in time, 7h)


@dataclass(frozen=True)
class PricePoint:
    day: date
    close: Decimal


def split_factor(splits: Sequence[tuple[date, Decimal]], after: date, upto: date) -> Decimal:
    """Product of split ratios with an ex-date in (after, upto]; 1 when there are none."""
    factor = Decimal(1)
    for ex_date, ratio in splits:
        if after < ex_date <= upto:
            factor *= ratio
    return factor


def shares_on(count: ShareCount, day: date, splits: Sequence[tuple[date, Decimal]]) -> Decimal:
    """The share count moved from its record date to `day` across any split in between."""
    if count.record_date <= day:
        return count.shares * split_factor(splits, count.record_date, day)
    return count.shares / split_factor(splits, day, count.record_date)


def count_as_of(counts: Sequence[ShareCount], as_of: date) -> ShareCount | None:
    """The latest share count dated and filed by `as_of` (never one reported later)."""
    known = [c for c in counts if c.record_date <= as_of and (c.filed is None or c.filed <= as_of)]
    return max(known, key=lambda c: c.record_date) if known else None


def share_count_for(counts: Sequence[ShareCount], year_end: date) -> ShareCount | None:
    """The cover-page count reported nearest after a year end (its 10-K), else the last before."""
    after = [c for c in counts if c.record_date >= year_end]
    if after:
        return min(after, key=lambda c: c.record_date)
    before = [c for c in counts if c.record_date < year_end]
    return max(before, key=lambda c: c.record_date) if before else None


def price_on_or_before(prices: Sequence[PricePoint], day: date) -> PricePoint | None:
    """The last close on or before `day`; `prices` sorted by day."""
    index = bisect_right([p.day for p in prices], day)
    return prices[index - 1] if index else None


def share_counts(session: Session, company_id: int) -> list[ShareCount]:
    facts = session.scalars(
        select(FinancialFact).where(
            FinancialFact.company_id == company_id, FinancialFact.concept_tag == SHARES_CONCEPT
        )
    ).all()
    # Multi-class filers report one value per class under the same filing and date (DR-025).
    groups: dict[tuple[str, date], list[FinancialFact]] = {}
    for fact in facts:
        if fact.period_end is not None:
            groups.setdefault((fact.accession_number, fact.period_end), []).append(fact)
    counts = []
    for (_, record_date), group in groups.items():
        total = total_shares_outstanding([f.value for f in group])
        if total:
            first = min(group, key=lambda f: f.id)
            filed = max((f.filed_date for f in group if f.filed_date), default=None)
            counts.append(ShareCount(record_date, total, f"xbrl:{first.id}", filed))
    return counts


def _compute(
    *,
    price: PricePoint,
    shares: Decimal | None,
    shares_source: str | None,
    value: dict[str, Decimal | None],
    source: dict[str, str],
    eps: Decimal | None,
    ev_applies: bool,
    net_income: Decimal | None = None,
) -> list[MetricResult]:
    def src(*names: str, **renamed: str) -> dict[str, str]:
        """Lineage keyed by the formula's own input names (`revenue_ttm=` maps from `revenue`)."""
        out = {n: source[n] for n in names if n in source}
        out.update({arg: source[n] for arg, n in renamed.items() if n in source})
        return out

    mc = metrics.market_cap(
        price.close,
        shares,
        sources={"shares_outstanding": shares_source} if shares_source else None,
    )
    ev = metrics.enterprise_value(
        mc.value,
        value.get("total_debt"),
        value.get("cash_and_equivalents"),
        value.get("short_term_investments"),
        applicable=ev_applies,
        sources=src("total_debt", "cash_and_equivalents", "short_term_investments"),
    )
    revenue, fcf, ebitda = value.get("revenue"), value.get("free_cash_flow"), value.get("ebitda")
    return [
        mc,
        ev,
        (
            metrics.pe_from_earnings(mc.value, net_income, sources=src(net_income_ttm="net_income"))
            if net_income is not None
            else metrics.pe_ratio(price.close, eps, sources=src(eps_diluted_ttm="eps_diluted"))
        ),
        metrics.ps_ratio(mc.value, revenue, sources=src(revenue_ttm="revenue")),
        metrics.p_fcf(mc.value, fcf, sources=src(fcf_ttm="free_cash_flow")),
        metrics.ev_to_revenue(
            ev.value, revenue, applicable=ev_applies, sources=src(revenue_ttm="revenue")
        ),
        metrics.ev_to_ebitda(
            ev.value, ebitda, applicable=ev_applies, sources=src(ebitda_ttm="ebitda")
        ),
        metrics.fcf_yield(fcf, mc.value, sources=src(fcf_ttm="free_cash_flow")),
    ]


def _persist(
    session: Session,
    company: Company,
    result: MetricResult,
    *,
    period: str,
    period_type: str,
    fiscal_year: int,
    period_end: date,
    basis: dict[str, str],
) -> None:
    name = _NAMES.get(result.formula_id, result.formula_id)
    metrics_repo.upsert_metric(
        session,
        company_id=company.id,
        period=period,
        fiscal_year=fiscal_year,
        fiscal_quarter=None,
        period_start=None,
        period_end=period_end,
        period_type=period_type,
        metric_name=name,
        metric_value=result.value,  # None = N/A (bank EV, EPS <= 0) or a missing input
        unit=result.unit,
        is_derived=True,
        formula_id=name,
        source_id=f"derived:{company.ticker}:{name}:{period}",
        as_reported_accession=_DERIVED_ACCESSION,
        is_latest=True,
        quality_flags={
            "inputs": [{"name": i.name, "source_id": i.source_id} for i in result.inputs],
            "formula_version": result.formula_version,
            "warnings": result.warnings,
            "basis": basis,  # FR-024: what the multiple was computed from
        },
    )


def load_splits(session: Session, company_id: int) -> list[tuple[date, Decimal]]:
    """A company's stock splits as (ex-date, ratio), for moving share counts across them."""
    return [
        (d, r)
        for d, r in session.execute(
            select(CorporateAction.ex_date, CorporateAction.ratio_or_amount).where(
                CorporateAction.company_id == company_id, CorporateAction.action_type == "split"
            )
        ).all()
        if d is not None and r
    ]


def build_company_valuation(session: Session, company: Company) -> int:
    """Rebuild a company's valuation rows (fiscal years + latest close). Returns rows written."""
    session.execute(
        delete(FinancialMetric).where(
            FinancialMetric.company_id == company.id,
            FinancialMetric.metric_name.in_(VALUATION_METRICS),
        )
    )
    rows = session.scalars(
        select(FinancialMetric).where(
            FinancialMetric.company_id == company.id,
            FinancialMetric.period_type == "FY",
            FinancialMetric.fiscal_year.is_not(None),
            FinancialMetric.is_latest.is_(True),
        )
    ).all()
    value: dict[int, dict[str, Decimal | None]] = {}
    source: dict[int, dict[str, str]] = {}
    year_end: dict[int, date] = {}
    for row in rows:
        fy = row.fiscal_year or 0  # never 0: the query keeps only rows with a fiscal year
        value.setdefault(fy, {})[row.metric_name] = row.metric_value
        if row.source_id:
            source.setdefault(fy, {})[row.metric_name] = row.source_id
        if row.period_end is not None:
            year_end[fy] = max(row.period_end, year_end.get(fy, row.period_end))

    prices = [
        PricePoint(d, c)
        for d, c in session.execute(
            select(PriceHistory.date, PriceHistory.close)
            .where(PriceHistory.company_id == company.id)
            .order_by(PriceHistory.date)
        ).all()
        if c is not None
    ]
    if not prices or not year_end:
        return 0
    splits = load_splits(session, company.id)
    counts = share_counts(session, company.id)
    cmap = load_concept_map(ticker=company.ticker, cik=company.cik, sector=company.sector)
    ev_applies = cmap.applies("ev_ebitda")  # EV-based multiples are N/A for banks (Appendix C)

    facts = with_composites(
        [fact_view_from_orm(f) for f in facts_repo.facts_for_company(session, company.id)],
        cmap.composites,
    )

    def run(fy: int, price: PricePoint, period: str, period_type: str) -> int:
        net_income: Decimal | None = None
        if period_type == "FY":
            count = share_count_for(counts, year_end[fy])
            figures, sources = value[fy], source.get(fy, {})
            eps = figures.get("eps_diluted")
            if eps is not None:
                # Reported EPS sits on the year end's share basis; restate it for any later split.
                eps = eps / split_factor(splits, year_end[fy], price.day)
            basis = {
                "fiscal_year": f"FY{fy}",
                "figures": "fiscal year: the twelve months to the year end",
            }
        else:
            count = count_as_of(counts, price.day)
            quarter_end, found = figures_as_of(facts, cmap, price.day)
            derived = derived_values(found)
            figures = {name: value for name, (value, _) in derived.items()}
            # One input row per figure, citing its first fact; `ttm_sources` lists them all.
            sources = {name: ids[0] for name, (_, ids) in derived.items() if ids}
            eps, net_income = None, figures.get("net_income")
            basis = {
                "fiscal_year": f"FY{fy}",
                "figures": f"TTM to {quarter_end.isoformat() if quarter_end else 'n/a'}, "
                "as filed by the price date",
                "ttm_sources": ";".join(
                    f"{n}={','.join(ids)}" for n, (_, ids) in sorted(derived.items())
                ),
            }
        shares = shares_on(count, price.day, splits) if count else None
        basis |= {"price_date": price.day.isoformat(), "price_close": str(price.close)}
        if count is not None and shares is not None:
            basis.update(shares=str(shares), shares_record_date=count.record_date.isoformat())
        results = _compute(
            price=price,
            shares=shares,
            shares_source=count.source_id if count else None,
            value=figures,
            source=sources,
            eps=eps,
            ev_applies=ev_applies,
            net_income=net_income,
        )
        for result in results:
            _persist(
                session,
                company,
                result,
                period=period,
                period_type=period_type,
                fiscal_year=fy,
                period_end=price.day,
                basis=basis,
            )
        return len(results)

    written = 0
    for fy in sorted(year_end):
        price = price_on_or_before(prices, year_end[fy])
        if price is not None:  # no price history that far back: nothing to value
            written += run(fy, price, f"FY{fy}", "FY")
    latest_fy = max(year_end)
    written += run(latest_fy, prices[-1], prices[-1].day.isoformat(), LATEST)
    return written
