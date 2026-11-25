"""Persist quarterly and TTM metrics and refresh coverage gaps (Phase 7b, ADR-0040).

Runs inside `build_company_metrics`, after the annual rows, over the same facts. For the last 12
fiscal quarters it writes every quarter of every flow metric and balance (`period_type` Q), then
for each complete four-quarter window the TTM sums and TTM ratios (`period_type` TTM). Source
IDs (FR-QT-007): `xbrl:{TICKER}:{metric}:Q{n}-FY{yr}` for a figure read directly from a filing,
`derived:{TICKER}:{metric}:Q{n}-FY{yr}` for a computed quarter, and
`derived:{TICKER}:{metric}:TTM-Q{n}-FY{yr}` for a TTM figure. Derived rows keep the existing
record format: `formula_id`, and `quality_flags` with the formula text (`derived`) and the
inputs' source IDs (`inputs`), so the citation drawer links every input (CIT-003).

Whatever cannot be computed is recorded in `metric_gaps` with a reason, never guessed and never
silently empty (FR-QT-004/010). A sector-inapplicable metric (DR-042) is N/A, not a gap.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.finance import metrics, ttm
from app.finance.concept_map import ConceptMap
from app.finance.quarterly import (
    YTD_NAME,
    FiscalQuarter,
    FiscalYear,
    QuarterContext,
    QuarterlySeries,
    QuarterValue,
    build_context,
    build_series,
    cite,
    kept_quarters,
    reports,
    source_id,
    ytd_fact,
)
from app.finance.selection import FactView
from app.models import Company
from app.repositories import gaps as gaps_repo
from app.repositories import metrics as metrics_repo
from app.schemas.sources import MetricInput, MetricResult

INCOMPLETE_WINDOW = "incomplete_window"  # a TTM window missing at least one quarter
_DERIVED_ACCESSION = "derived"  # non-null sentinel so the upsert key is idempotent (as ratios)
_EPS = "eps_diluted"
_SHARES = "shares_diluted_weighted"

# Formula text for TTM figures derived from other TTM figures (DR-QT-003).
_TTM_FORMULAS = {
    "free_cash_flow": "TTM operating cash flow - TTM capex",
    "ebitda": "TTM operating income + TTM depreciation & amortization",
    "effective_tax_rate": "TTM income tax expense / TTM pretax income",
    "gross_margin": "TTM gross profit / TTM revenue",
    "operating_margin": "TTM operating income / TTM revenue",
    "fcf_margin": "TTM free cash flow / TTM revenue",
    # Earnings quality on a trailing basis (Phase 7d, FR-EQ-008).
    "fcf_conversion": "TTM free cash flow / TTM net income",
    "accruals_ratio": (
        "(TTM net income - TTM operating cash flow) / average total assets at the window's start "
        "and end"
    ),
    "roic": "TTM operating income x (1 - TTM tax rate) / (debt + equity - cash) at quarter end",
    "net_margin": "TTM net income / TTM revenue",
    "roe": "TTM net income / shareholders' equity at quarter end",
    "roa": "TTM net income / total assets at quarter end",
}
# A quarter's own margins (its flows over its revenue). A quarter's ROE is not stored: one quarter's
# profit over year-end-style equity reads as a quarter of the annual figure (TTM ROE is stored).
_QUARTER_MARGINS = {
    "gross_margin": "Q gross profit / Q revenue",
    "operating_margin": "Q operating income / Q revenue",
    "net_margin": "Q net income / Q revenue",
}


@dataclass
class QuarterlyCounts:
    quarterly: int = 0
    ttm: int = 0
    gaps: int = 0


def quarterly_metrics(cmap: ConceptMap) -> list[str]:
    """Every flow metric, and the balance-sheet stocks (the cover-page share count is dated
    after the quarter, not at its end, so it has no quarterly balance)."""
    stocks = [m for m in cmap.stock_metrics if cmap.unit_family(m) == "currency"]
    return [m for m in cmap.flow_metrics if not cmap.is_annual_only(m)] + stocks


def coverage_metrics(cmap: ConceptMap) -> list[str]:
    """Every metric this builder can store, in display order, for the coverage endpoint."""
    return [*quarterly_metrics(cmap), "total_debt", *_TTM_FORMULAS]


def ttm_summed(cmap: ConceptMap) -> list[str]:
    """Flows whose TTM is a four-quarter sum: amounts, not per-share figures or share counts."""
    return [
        m
        for m in cmap.flow_metrics
        if cmap.unit_family(m) == "currency" and not cmap.is_annual_only(m)
    ]


class _Writer:
    def __init__(self, session: Session, company: Company) -> None:
        self.session = session
        self.company = company
        self.counts = QuarterlyCounts()

    def quarter(self, metric: str, value: QuarterValue) -> None:
        flags: dict[str, Any] | None = None
        if value.is_derived:
            flags = {
                "derived": value.formula,
                "inputs": [
                    {"name": i.name, "source_id": i.source_id, "accession": i.accession}
                    for i in value.inputs
                ],
            }
        metrics_repo.upsert_metric(
            self.session,
            company_id=self.company.id,
            period=value.quarter.label,
            fiscal_year=value.quarter.fiscal_year,
            fiscal_quarter=value.quarter.quarter,
            period_start=value.period_start,
            period_end=value.period_end,
            period_type="Q",
            metric_name=metric,
            metric_value=value.value,
            unit=value.unit,
            is_derived=value.is_derived,
            source_tag=value.source_tag,
            formula_id=value.formula_id,
            source_id=value.source_id,
            as_reported_accession=value.accession or _DERIVED_ACCESSION,
            is_latest=True,
            quality_flags=flags,
        )
        self.counts.quarterly += 1

    def derived(
        self,
        metric: str,
        quarter: FiscalQuarter,
        period_end: date | None,
        result: MetricResult,
        *,
        ttm_row: bool,
        formula: str,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Persist a computed figure (ratio record format, DR-040). Returns its source ID."""
        label = f"TTM-{quarter.label}" if ttm_row else quarter.label
        sid = source_id(self.company.ticker, metric, label, derived=True)
        metrics_repo.upsert_metric(
            self.session,
            company_id=self.company.id,
            period=label,
            fiscal_year=quarter.fiscal_year,
            fiscal_quarter=quarter.quarter,
            period_start=None,
            period_end=period_end,
            period_type="TTM" if ttm_row else "Q",
            metric_name=metric,
            metric_value=result.value,  # None = N/A or uncomputable, with a reason (DR-041/042)
            unit=result.unit,
            is_derived=True,
            formula_id=result.formula_id,
            source_id=sid,
            as_reported_accession=_DERIVED_ACCESSION,
            is_latest=True,
            quality_flags={
                "derived": formula,
                # Only figures the result was computed from; an unreported optional operand
                # (a debt component the company does not tag) is not an input to cite.
                "inputs": [
                    {"name": i.name, "source_id": i.source_id}
                    for i in result.inputs
                    if i.value is not None
                ],
                "formula_version": result.formula_version,
                "warnings": result.warnings,
                **(extra or {}),
            },
        )
        if ttm_row:
            self.counts.ttm += 1
        else:
            self.counts.quarterly += 1
        return sid


def build_quarterly_metrics(
    session: Session,
    company: Company,
    facts: Sequence[FactView],
    cmap: ConceptMap,
    annual_spans: Sequence[tuple[date, date]],
    fye_month: int,
) -> QuarterlyCounts:
    """Persist the last 12 quarters, every complete TTM window, and the company's gaps."""
    ctx = build_context(company.ticker, cmap, facts, annual_spans, fye_month)
    kept = kept_quarters(ctx.ends)
    writer = _Writer(session, company)
    gaps: list[tuple[str, str, str]] = []

    series: dict[str, QuarterlySeries] = {}
    for metric in quarterly_metrics(cmap):
        series[metric] = built = build_series(ctx, metric, kept)
        for value in built.values.values():
            writer.quarter(metric, value)
        if _gaps_apply(ctx, metric):
            gaps.extend((metric, q.label, reason) for q, reason in built.gaps.items())

    _quarter_margins(writer, ctx, series, kept)
    debt = _total_debt(writer, ctx, series, kept)
    # A TTM window must lie inside the kept quarters, so every input it cites is stored.
    for as_of in kept[ttm.TTM_QUARTERS - 1 :]:
        gaps.extend(_ttm_window(writer, ctx, series, debt, as_of))

    writer.counts.gaps = gaps_repo.replace_company_gaps(session, company.id, gaps)
    return writer.counts


def _quarter_margins(
    writer: _Writer,
    ctx: QuarterContext,
    series: dict[str, QuarterlySeries],
    kept: Sequence[FiscalQuarter],
) -> None:
    """Each kept quarter's margins from that quarter's own figures (§10.3), cited to them.
    Stored when computable or N/A for the sector; a missing input is the input's own gap."""
    cmap = ctx.concept_map
    for quarter in kept:
        end = ctx.ends.get(quarter)
        if end is None:
            continue
        found: dict[str, QuarterValue | None] = {
            m: series[m].values.get(quarter)
            for m in (
                "revenue",
                "gross_profit",
                "cost_of_revenue",
                "operating_income",
                "net_income",
            )
            if m in series
        }

        def v(metric: str, found: dict[str, QuarterValue | None] = found) -> metrics.Num:
            value = found.get(metric)
            return value.value if value else None

        sources: dict[str, str] = {m: q.source_id for m, q in found.items() if q is not None}
        for result in (
            metrics.gross_margin(
                v("gross_profit"),
                v("revenue"),
                v("cost_of_revenue"),
                applicable=cmap.applies("gross_margin"),
                sources=sources,
            ),
            metrics.operating_margin(v("operating_income"), v("revenue"), sources=sources),
            metrics.net_margin(v("net_income"), v("revenue"), sources=sources),
        ):
            if result.value is not None or metrics.MISSING_INPUT not in result.warnings:
                formula = _QUARTER_MARGINS[result.formula_id]
                writer.derived(
                    result.formula_id, quarter, end, result, ttm_row=False, formula=formula
                )


def _gaps_apply(ctx: QuarterContext, metric: str) -> bool:
    """N/A for the sector, or never reported at all, is not a missing quarter (FR-QT-010)."""
    return ctx.concept_map.applies(metric) and reports(ctx, metric)


def _values(series: dict[str, QuarterlySeries], metric: str) -> dict[FiscalQuarter, QuarterValue]:
    found = series.get(metric)
    return found.values if found else {}


def _total_debt(
    writer: _Writer,
    ctx: QuarterContext,
    series: dict[str, QuarterlySeries],
    kept: Sequence[FiscalQuarter],
) -> dict[FiscalQuarter, tuple[MetricResult, str]]:
    """Quarter-end total debt (short + long term), stored so TTM ROIC can cite it."""
    short, long = _values(series, "short_term_debt"), _values(series, "long_term_debt")
    out: dict[FiscalQuarter, tuple[MetricResult, str]] = {}
    for quarter in kept:
        short_q, long_q = short.get(quarter), long.get(quarter)
        if short_q is None and long_q is None:
            continue
        result = metrics.total_debt(
            short_q.value if short_q else None,
            long_q.value if long_q else None,
            sources={
                name: q.source_id
                for name, q in (("short_term_debt", short_q), ("long_term_debt", long_q))
                if q
            },
        )
        sid = writer.derived(
            "total_debt",
            quarter,
            ctx.ends[quarter],
            result,
            ttm_row=False,
            formula="total_debt = short-term debt + long-term debt",
        )
        out[quarter] = (result, sid)
    return out


def _ttm_window(
    writer: _Writer,
    ctx: QuarterContext,
    series: dict[str, QuarterlySeries],
    debt: dict[FiscalQuarter, tuple[MetricResult, str]],
    as_of: FiscalQuarter,
) -> list[tuple[str, str, str]]:
    """Every TTM figure ending at `as_of`; returns the gaps of TTM figures that cannot exist."""
    cmap = ctx.concept_map
    label = f"TTM-{as_of.label}"
    end = ctx.ends.get(as_of)  # None: no filing covers that quarter, so nothing can be summed
    gaps: list[tuple[str, str, str]] = []
    totals: dict[str, MetricResult] = {}
    sources: dict[str, str] = {}

    def keep(
        metric: str, result: MetricResult, formula: str, extra: dict[str, Any] | None = None
    ) -> None:
        sources[metric] = writer.derived(
            metric, as_of, end, result, ttm_row=True, formula=formula, extra=extra
        )
        totals[metric] = result

    window = ttm.window(as_of)
    sum_formula = "TTM = " + " + ".join(q.label for q in window)
    for metric in ttm_summed(cmap):
        result = ttm.ttm_for(_values(series, metric), as_of, kind="flow")
        if result.value is not None:
            keep(metric, result, sum_formula)
        elif _gaps_apply(ctx, metric):
            gaps.append((metric, label, INCOMPLETE_WINDOW))  # never annualized (DR-QT-002)

    if cmap.kind(_EPS) == "flow":
        eps = _ttm_eps(ctx, series, as_of, sum_formula)
        if eps is not None:
            eps_result, eps_formula, eps_flags = eps
            keep(_EPS, eps_result, eps_formula, eps_flags)
        elif _gaps_apply(ctx, _EPS):
            gaps.append((_EPS, label, INCOMPLETE_WINDOW))

    if end is not None:
        _ttm_ratios(ctx, series, debt, as_of, totals, sources, keep)
    return gaps


def _ttm_eps(
    ctx: QuarterContext, series: dict[str, QuarterlySeries], as_of: FiscalQuarter, sum_formula: str
) -> tuple[MetricResult, str, dict[str, Any]] | None:
    """TTM diluted EPS with its formula text and quality flag, or None (FR-QT-008/009)."""
    quarterly = _values(series, _EPS)
    unit = next((v.unit for v in quarterly.values()), "USD/shares")
    identity = _eps_identity(ctx, as_of)
    result = ttm.ttm_eps(quarterly, as_of, identity, unit=unit)
    if result.value is None:
        return None
    formula = sum_formula if result.formula_id == "ttm_sum" else ttm.identity_formula(identity)
    shares = _values(series, _SHARES)
    extra: dict[str, Any] = {}
    if ttm.share_count_changed(
        [shares[q].value if q in shares else None for q in ttm.window(as_of)]
    ):
        # Summed across a >5% share-count change: said on the row, never hidden (FR-QT-009).
        result = result.model_copy(update={"warnings": [*result.warnings, ttm.SHARE_COUNT_CHANGED]})
        extra["quality_flag"] = ttm.SHARE_COUNT_CHANGED
    return result, formula, extra


def _eps_identity(ctx: QuarterContext, as_of: FiscalQuarter) -> list[tuple[int, ttm.Cited | None]]:
    """FY_prior - YTD_prior + YTD_current; at a fiscal year end, the year's own EPS.

    Each term is its period's latest-filed figure, so all three share one share basis: a stock
    split between the prior 10-K and today is restated in later filings, and mixing an as-filed
    pre-split figure with post-split ones is off by the split ratio (NVIDIA's 2024 10:1 split
    made TTM EPS negative that way). Terms cite the exact raw fact used (`xbrl:<fact_id>`).
    """
    year = ctx.years.get(as_of.fiscal_year)
    prior = ctx.years.get(as_of.fiscal_year - 1)

    def term(y: FiscalYear | None, quarter: int) -> ttm.Cited | None:
        if y is None or (quarter == 4 and y.end is None):
            return None
        found = ytd_fact(ctx, _EPS, y, quarter, latest_filed=True)
        if found is None:
            return None
        name = f"{YTD_NAME[quarter]}{'' if quarter == 4 else ' FY'}{y.fiscal_year}"
        return ttm.Cited(name, found[1].value, cite(found[1], name).source_id)

    if as_of.quarter == 4:
        return [(1, term(year, 4))]
    return [(1, term(prior, 4)), (-1, term(prior, as_of.quarter)), (1, term(year, as_of.quarter))]


def _ttm_ratios(
    ctx: QuarterContext,
    series: dict[str, QuarterlySeries],
    debt: dict[FiscalQuarter, tuple[MetricResult, str]],
    as_of: FiscalQuarter,
    totals: dict[str, MetricResult],
    sources: dict[str, str],
    keep: Callable[[str, MetricResult, str], None],
) -> None:
    """TTM figures computed from TTM numerators and denominators, never averaged quarterly
    ratios (DR-QT-003). Stored only when computable or N/A for the sector; a missing input
    means the TTM itself is missing, which its own gap already says."""
    cmap = ctx.concept_map

    def v(metric: str) -> metrics.Num:
        found = totals.get(metric)
        return found.value if found else None

    def store(result: MetricResult) -> None:
        if result.value is not None or metrics.MISSING_INPUT not in result.warnings:
            keep(result.formula_id, result, _TTM_FORMULAS[result.formula_id])

    for result in (
        metrics.free_cash_flow(v("operating_cash_flow"), v("capex"), sources=sources),
        metrics.ebitda(v("operating_income"), v("depreciation_amortization"), sources=sources),
        metrics.effective_tax_rate(v("income_tax_expense"), v("pretax_income"), sources=sources),
    ):
        store(result)
    store(
        metrics.gross_margin(
            v("gross_profit"),
            v("revenue"),
            v("cost_of_revenue"),
            applicable=cmap.applies("gross_margin"),
            sources=sources,
        )
    )
    store(metrics.operating_margin(v("operating_income"), v("revenue"), sources=sources))
    store(metrics.fcf_margin(v("free_cash_flow"), v("revenue"), sources=sources))
    store(metrics.fcf_conversion(v("free_cash_flow"), v("net_income"), sources=sources))
    store(metrics.net_margin(v("net_income"), v("revenue"), sources=sources))
    store(_ttm_accruals(series, as_of, v, sources))

    # Balances are the latest quarter end's, i.e. their TTM is the balance (FR-QT-008).
    equity = ttm.ttm_for(_values(series, "shareholders_equity"), as_of, kind="stock")
    cash = ttm.ttm_for(_values(series, "cash_and_equivalents"), as_of, kind="stock")
    total_debt, debt_id = debt.get(as_of, (None, None))
    balance_sources = {
        "tax_rate": sources.get("effective_tax_rate"),
        "total_debt": debt_id,
        "shareholders_equity": equity.inputs[0].source_id,
        "cash_and_equivalents": cash.inputs[0].source_id,
    }
    assets = ttm.ttm_for(_values(series, "total_assets"), as_of, kind="stock")
    if equity.inputs and (equity_id := equity.inputs[0].source_id):
        roe_src = {**sources, "shareholders_equity": equity_id}
        store(metrics.roe(v("net_income"), equity.value, sources=roe_src))
    if assets.inputs and (assets_id := assets.inputs[0].source_id):
        roa_src = {**sources, "total_assets": assets_id}
        store(metrics.roa(v("net_income"), assets.value, sources=roa_src))
    store(
        metrics.roic(
            v("operating_income"),
            v("effective_tax_rate"),
            total_debt.value if total_debt else None,
            equity.value,
            cash.value,
            applicable=cmap.applies("roic"),
            sources={**sources, **{k: s for k, s in balance_sources.items() if s}},
        )
    )


def _ttm_accruals(
    series: dict[str, QuarterlySeries],
    as_of: FiscalQuarter,
    v: Callable[[str], metrics.Num],
    sources: dict[str, str],
) -> MetricResult:
    """TTM accruals ratio over the average of total assets before and at the end of the window.
    The opening balance is the quarter before the window; without it there is no ratio."""
    assets = _values(series, "total_assets")
    opening = ttm.ttm_for(assets, as_of.trailing(ttm.TTM_QUARTERS + 1)[0], kind="stock")
    closing = ttm.ttm_for(assets, as_of, kind="stock")
    average = (
        None
        if opening.value is None or closing.value is None
        else (opening.value + closing.value) / 2
    )
    result = metrics.accruals_ratio(
        v("net_income"), v("operating_cash_flow"), average, sources=sources
    )
    balances = [
        MetricInput(name=f"{label}_total_assets", value=b.value, source_id=b.inputs[0].source_id)
        for label, b in (("opening", opening), ("closing", closing))
    ]
    return result.model_copy(update={"inputs": [*result.inputs, *balances]})
