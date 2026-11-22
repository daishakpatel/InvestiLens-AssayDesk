"""Deterministic financial metric formulas (spec §12). Pure functions, no I/O, no LLM.

Every function takes numbers in and returns a `MetricResult(value, unit, inputs, formula_id,
warnings)` (DR-040). Division never happens on a zero/negative base silently — a `None` value is
returned with a reason code (`MISSING_INPUT`, `NEGATIVE_BASE`, `NOT_APPLICABLE_SECTOR`) in
`warnings` (DR-041). Money/ratios are `Decimal` throughout; no float ever enters the calculation
path. Changing a formula bumps `FORMULA_VERSION` (DR-043).

Sector applicability (DR-042) is passed in as `applicable` by the caller (which looks it up in
`sector_applicability.yaml` via the concept map) so this module stays pure and branch-free of
`if sector == "bank"`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Any, Literal, cast

from app.schemas.sources import MetricInput, MetricResult

FORMULA_VERSION = "v1"

# Units
RATIO = "ratio"
USD = "USD"
DAYS = "days"
SHARES = "shares"
SCORE = "score"
MULTIPLE = "x"

# Reason codes (DR-041)
MISSING_INPUT = "MISSING_INPUT"
NEGATIVE_BASE = "NEGATIVE_BASE"
NOT_APPLICABLE_SECTOR = "NOT_APPLICABLE_SECTOR"

Num = Decimal | None
Sources = Mapping[str, str] | None


def _inputs(pairs: Sequence[tuple[str, Num]], sources: Sources) -> list[MetricInput]:
    src = sources or {}
    return [MetricInput(name=name, value=value, source_id=src.get(name)) for name, value in pairs]


def _ok(value: Decimal, unit: str, formula_id: str, inputs: list[MetricInput]) -> MetricResult:
    return MetricResult(
        value=value,
        unit=unit,
        inputs=inputs,
        formula_id=formula_id,
        formula_version=FORMULA_VERSION,
    )


def _fail(reason: str, unit: str, formula_id: str, inputs: list[MetricInput]) -> MetricResult:
    return MetricResult(
        value=None,
        unit=unit,
        inputs=inputs,
        formula_id=formula_id,
        formula_version=FORMULA_VERSION,
        warnings=[reason],
    )


def _divide(
    numerator: Num,
    denominator: Num,
    *,
    unit: str,
    formula_id: str,
    inputs: list[MetricInput],
    positive_base: bool = True,
) -> MetricResult:
    """Guarded division (DR-041). Missing operand -> MISSING_INPUT; bad base -> NEGATIVE_BASE."""
    if numerator is None or denominator is None:
        return _fail(MISSING_INPUT, unit, formula_id, inputs)
    if denominator == 0 or (positive_base and denominator < 0):
        return _fail(NEGATIVE_BASE, unit, formula_id, inputs)
    return _ok(numerator / denominator, unit, formula_id, inputs)


def _sum(
    values: Sequence[Num],
    *,
    unit: str,
    formula_id: str,
    inputs: list[MetricInput],
    require_any: bool = True,
) -> MetricResult:
    present = [v for v in values if v is not None]
    if require_any and not present:
        return _fail(MISSING_INPUT, unit, formula_id, inputs)
    return _ok(sum(present, Decimal(0)), unit, formula_id, inputs)


# --- Growth & aggregate ---------------------------------------------------------------
def yoy_growth(current: Num, prior: Num, *, sources: Sources = None) -> MetricResult:
    inputs = _inputs([("current", current), ("prior", prior)], sources)
    return _divide(
        None if current is None or prior is None else current - prior,
        prior,
        unit=RATIO,
        formula_id="yoy_growth",
        inputs=inputs,
    )


# A "5-year average" over fewer valued years than this is not shown (loss years have no P/E).
MIN_AVERAGE_YEARS = 3


def average(values: Sequence[Num], *, unit: str) -> MetricResult:
    """Mean of the valued years (spec §10.7 "5Y Average P/E"); fewer than 3 is MISSING_INPUT."""
    inputs = _inputs([(f"y{i}", v) for i, v in enumerate(values)], None)
    present = [v for v in values if v is not None]
    if len(present) < MIN_AVERAGE_YEARS:
        return _fail(MISSING_INPUT, unit, "average", inputs)
    return _ok(sum(present, Decimal(0)) / len(present), unit, "average", inputs)


def cagr(start: Num, end: Num, years: int, *, sources: Sources = None) -> MetricResult:
    inputs = _inputs([("start", start), ("end", end)], sources)
    if start is None or end is None:
        return _fail(MISSING_INPUT, RATIO, "cagr", inputs)
    if start <= 0 or end <= 0 or years <= 0:
        return _fail(NEGATIVE_BASE, RATIO, "cagr", inputs)
    value = (end / start) ** (Decimal(1) / Decimal(years)) - 1
    return _ok(value, RATIO, "cagr", inputs)


def ttm_sum(
    quarters: Sequence[Num],
    *,
    sources: Sources = None,
    labels: Sequence[str] | None = None,
    unit: str = USD,
) -> MetricResult:
    """Trailing-twelve-months sum of the last four quarterly flow values.

    `labels` names the inputs by their fiscal quarter ("Q3-FY2025"); the default is q1..qN.
    Fewer than four quarters is MISSING_INPUT, never an annualized guess (DR-QT-002).
    """
    names = list(labels) if labels is not None else [f"q{i}" for i in range(1, len(quarters) + 1)]
    inputs = _inputs(list(zip(names, quarters, strict=True)), sources)
    if len(quarters) < 4 or any(v is None for v in quarters[-4:]):
        return _fail(MISSING_INPUT, unit, "ttm_sum", inputs)
    return _ok(
        sum((v for v in quarters[-4:] if v is not None), Decimal(0)), unit, "ttm_sum", inputs
    )


# --- Profitability --------------------------------------------------------------------
def gross_margin(
    gross_profit: Num,
    revenue: Num,
    cost_of_revenue: Num = None,
    *,
    applicable: bool = True,
    sources: Sources = None,
) -> MetricResult:
    inputs = _inputs(
        [
            ("gross_profit", gross_profit),
            ("revenue", revenue),
            ("cost_of_revenue", cost_of_revenue),
        ],
        sources,
    )
    if not applicable:
        return _fail(NOT_APPLICABLE_SECTOR, RATIO, "gross_margin", inputs)
    profit = gross_profit
    if profit is None and revenue is not None and cost_of_revenue is not None:
        profit = revenue - cost_of_revenue
    return _divide(profit, revenue, unit=RATIO, formula_id="gross_margin", inputs=inputs)


def operating_margin(
    operating_income: Num, revenue: Num, *, sources: Sources = None
) -> MetricResult:
    inputs = _inputs([("operating_income", operating_income), ("revenue", revenue)], sources)
    return _divide(
        operating_income, revenue, unit=RATIO, formula_id="operating_margin", inputs=inputs
    )


def net_margin(net_income: Num, revenue: Num, *, sources: Sources = None) -> MetricResult:
    inputs = _inputs([("net_income", net_income), ("revenue", revenue)], sources)
    return _divide(net_income, revenue, unit=RATIO, formula_id="net_margin", inputs=inputs)


def ebitda(
    operating_income: Num, depreciation_amortization: Num, *, sources: Sources = None
) -> MetricResult:
    """EBITDA = operating income + D&A, only if BOTH are present (DR-010)."""
    inputs = _inputs(
        [
            ("operating_income", operating_income),
            ("depreciation_amortization", depreciation_amortization),
        ],
        sources,
    )
    if operating_income is None or depreciation_amortization is None:
        return _fail(MISSING_INPUT, USD, "ebitda", inputs)
    return _ok(operating_income + depreciation_amortization, USD, "ebitda", inputs)


def free_cash_flow(
    operating_cash_flow: Num, capex: Num, *, sources: Sources = None
) -> MetricResult:
    """FCF = operating cash flow - capex (capex is a positive cash outflow)."""
    inputs = _inputs([("operating_cash_flow", operating_cash_flow), ("capex", capex)], sources)
    if operating_cash_flow is None or capex is None:
        return _fail(MISSING_INPUT, USD, "free_cash_flow", inputs)
    return _ok(operating_cash_flow - capex, USD, "free_cash_flow", inputs)


def fcf_margin(free_cash_flow_value: Num, revenue: Num, *, sources: Sources = None) -> MetricResult:
    inputs = _inputs([("free_cash_flow", free_cash_flow_value), ("revenue", revenue)], sources)
    return _divide(
        free_cash_flow_value, revenue, unit=RATIO, formula_id="fcf_margin", inputs=inputs
    )


def fcf_conversion(
    free_cash_flow_value: Num, net_income: Num, *, sources: Sources = None
) -> MetricResult:
    inputs = _inputs(
        [("free_cash_flow", free_cash_flow_value), ("net_income", net_income)], sources
    )
    return _divide(
        free_cash_flow_value, net_income, unit=RATIO, formula_id="fcf_conversion", inputs=inputs
    )


# --- Balance sheet --------------------------------------------------------------------
def total_debt(
    short_term_debt: Num,
    long_term_debt: Num,
    current_portion_ltd: Num = None,
    finance_leases: Num = None,
    *,
    sources: Sources = None,
) -> MetricResult:
    inputs = _inputs(
        [
            ("short_term_debt", short_term_debt),
            ("long_term_debt", long_term_debt),
            ("current_portion_ltd", current_portion_ltd),
            ("finance_leases", finance_leases),
        ],
        sources,
    )
    return _sum(
        [short_term_debt, long_term_debt, current_portion_ltd, finance_leases],
        unit=USD,
        formula_id="total_debt",
        inputs=inputs,
    )


def net_cash(
    cash_and_equivalents: Num,
    short_term_investments: Num,
    total_debt_value: Num,
    *,
    sources: Sources = None,
) -> MetricResult:
    inputs = _inputs(
        [
            ("cash_and_equivalents", cash_and_equivalents),
            ("short_term_investments", short_term_investments),
            ("total_debt", total_debt_value),
        ],
        sources,
    )
    if cash_and_equivalents is None or total_debt_value is None:
        return _fail(MISSING_INPUT, USD, "net_cash", inputs)
    sti = short_term_investments or Decimal(0)
    return _ok(cash_and_equivalents + sti - total_debt_value, USD, "net_cash", inputs)


def cash_to_debt(
    cash_and_equivalents: Num,
    short_term_investments: Num,
    total_debt_value: Num,
    *,
    sources: Sources = None,
) -> MetricResult:
    inputs = _inputs(
        [
            ("cash_and_equivalents", cash_and_equivalents),
            ("short_term_investments", short_term_investments),
            ("total_debt", total_debt_value),
        ],
        sources,
    )
    if cash_and_equivalents is None:
        return _fail(MISSING_INPUT, RATIO, "cash_to_debt", inputs)
    numerator = cash_and_equivalents + (short_term_investments or Decimal(0))
    return _divide(
        numerator, total_debt_value, unit=RATIO, formula_id="cash_to_debt", inputs=inputs
    )


def current_ratio(
    current_assets: Num,
    current_liabilities: Num,
    *,
    applicable: bool = True,
    sources: Sources = None,
) -> MetricResult:
    inputs = _inputs(
        [("current_assets", current_assets), ("current_liabilities", current_liabilities)], sources
    )
    if not applicable:
        return _fail(NOT_APPLICABLE_SECTOR, RATIO, "current_ratio", inputs)
    return _divide(
        current_assets, current_liabilities, unit=RATIO, formula_id="current_ratio", inputs=inputs
    )


def quick_ratio(
    cash_and_equivalents: Num,
    short_term_investments: Num,
    receivables: Num,
    current_liabilities: Num,
    *,
    applicable: bool = True,
    sources: Sources = None,
) -> MetricResult:
    inputs = _inputs(
        [
            ("cash_and_equivalents", cash_and_equivalents),
            ("short_term_investments", short_term_investments),
            ("receivables", receivables),
            ("current_liabilities", current_liabilities),
        ],
        sources,
    )
    if not applicable:
        return _fail(NOT_APPLICABLE_SECTOR, RATIO, "quick_ratio", inputs)
    if cash_and_equivalents is None:
        return _fail(MISSING_INPUT, RATIO, "quick_ratio", inputs)
    numerator = (
        cash_and_equivalents + (short_term_investments or Decimal(0)) + (receivables or Decimal(0))
    )
    return _divide(
        numerator, current_liabilities, unit=RATIO, formula_id="quick_ratio", inputs=inputs
    )


def debt_to_equity(
    total_debt_value: Num, shareholders_equity: Num, *, sources: Sources = None
) -> MetricResult:
    inputs = _inputs(
        [("total_debt", total_debt_value), ("shareholders_equity", shareholders_equity)], sources
    )
    return _divide(
        total_debt_value,
        shareholders_equity,
        unit=RATIO,
        formula_id="debt_to_equity",
        inputs=inputs,
    )


# --- Valuation (price-dependent; Phase 1d supplies live prices) ------------------------
def market_cap(price: Num, shares_outstanding: Num, *, sources: Sources = None) -> MetricResult:
    inputs = _inputs([("price", price), ("shares_outstanding", shares_outstanding)], sources)
    if price is None or shares_outstanding is None:
        return _fail(MISSING_INPUT, USD, "market_cap", inputs)
    return _ok(price * shares_outstanding, USD, "market_cap", inputs)


def enterprise_value(
    market_cap_value: Num,
    total_debt_value: Num,
    cash_and_equivalents: Num,
    short_term_investments: Num = None,
    preferred: Num = None,
    noncontrolling_interest: Num = None,
    *,
    applicable: bool = True,
    sources: Sources = None,
) -> MetricResult:
    inputs = _inputs(
        [
            ("market_cap", market_cap_value),
            ("total_debt", total_debt_value),
            ("cash_and_equivalents", cash_and_equivalents),
            ("short_term_investments", short_term_investments),
            ("preferred", preferred),
            ("noncontrolling_interest", noncontrolling_interest),
        ],
        sources,
    )
    if not applicable:
        return _fail(NOT_APPLICABLE_SECTOR, USD, "enterprise_value", inputs)
    if market_cap_value is None or total_debt_value is None or cash_and_equivalents is None:
        return _fail(MISSING_INPUT, USD, "enterprise_value", inputs)
    value = (
        market_cap_value
        + total_debt_value
        + (preferred or Decimal(0))
        + (noncontrolling_interest or Decimal(0))
        - cash_and_equivalents
        - (short_term_investments or Decimal(0))
    )
    return _ok(value, USD, "enterprise_value", inputs)


def pe_ratio(price: Num, eps_diluted_ttm: Num, *, sources: Sources = None) -> MetricResult:
    inputs = _inputs([("price", price), ("eps_diluted_ttm", eps_diluted_ttm)], sources)
    return _divide(price, eps_diluted_ttm, unit=MULTIPLE, formula_id="pe_ratio", inputs=inputs)


def ps_ratio(market_cap_value: Num, revenue_ttm: Num, *, sources: Sources = None) -> MetricResult:
    inputs = _inputs([("market_cap", market_cap_value), ("revenue_ttm", revenue_ttm)], sources)
    return _divide(
        market_cap_value, revenue_ttm, unit=MULTIPLE, formula_id="ps_ratio", inputs=inputs
    )


def p_fcf(market_cap_value: Num, fcf_ttm: Num, *, sources: Sources = None) -> MetricResult:
    inputs = _inputs([("market_cap", market_cap_value), ("fcf_ttm", fcf_ttm)], sources)
    return _divide(market_cap_value, fcf_ttm, unit=MULTIPLE, formula_id="p_fcf", inputs=inputs)


def ev_to_revenue(
    enterprise_value_v: Num, revenue_ttm: Num, *, applicable: bool = True, sources: Sources = None
) -> MetricResult:
    inputs = _inputs(
        [("enterprise_value", enterprise_value_v), ("revenue_ttm", revenue_ttm)], sources
    )
    if not applicable:  # EV itself is N/A for banks (§12), so is every multiple of it
        return _fail(NOT_APPLICABLE_SECTOR, MULTIPLE, "ev_to_revenue", inputs)
    return _divide(
        enterprise_value_v, revenue_ttm, unit=MULTIPLE, formula_id="ev_to_revenue", inputs=inputs
    )


def ev_to_ebitda(
    enterprise_value_v: Num, ebitda_ttm: Num, *, applicable: bool = True, sources: Sources = None
) -> MetricResult:
    inputs = _inputs(
        [("enterprise_value", enterprise_value_v), ("ebitda_ttm", ebitda_ttm)], sources
    )
    if not applicable:
        return _fail(NOT_APPLICABLE_SECTOR, MULTIPLE, "ev_to_ebitda", inputs)
    return _divide(
        enterprise_value_v, ebitda_ttm, unit=MULTIPLE, formula_id="ev_to_ebitda", inputs=inputs
    )


def fcf_yield(fcf_ttm: Num, market_cap_value: Num, *, sources: Sources = None) -> MetricResult:
    inputs = _inputs([("fcf_ttm", fcf_ttm), ("market_cap", market_cap_value)], sources)
    return _divide(fcf_ttm, market_cap_value, unit=RATIO, formula_id="fcf_yield", inputs=inputs)


# --- Efficiency -----------------------------------------------------------------------
def _days_metric(
    numerator: Num, denominator: Num, days: int, formula_id: str, inputs: list[MetricInput]
) -> MetricResult:
    base = _divide(numerator, denominator, unit=DAYS, formula_id=formula_id, inputs=inputs)
    if base.value is None:
        return base
    return _ok(base.value * Decimal(days), DAYS, formula_id, inputs)


def dso(
    avg_receivables: Num, revenue: Num, *, days: int = 365, sources: Sources = None
) -> MetricResult:
    inputs = _inputs([("avg_receivables", avg_receivables), ("revenue", revenue)], sources)
    return _days_metric(avg_receivables, revenue, days, "dso", inputs)


def dio(
    avg_inventory: Num, cost_of_revenue: Num, *, days: int = 365, sources: Sources = None
) -> MetricResult:
    inputs = _inputs(
        [("avg_inventory", avg_inventory), ("cost_of_revenue", cost_of_revenue)], sources
    )
    return _days_metric(avg_inventory, cost_of_revenue, days, "dio", inputs)


def dpo(
    avg_payables: Num, cost_of_revenue: Num, *, days: int = 365, sources: Sources = None
) -> MetricResult:
    inputs = _inputs(
        [("avg_payables", avg_payables), ("cost_of_revenue", cost_of_revenue)], sources
    )
    return _days_metric(avg_payables, cost_of_revenue, days, "dpo", inputs)


def cash_conversion_cycle(
    dso_v: Num, dio_v: Num, dpo_v: Num, *, sources: Sources = None
) -> MetricResult:
    inputs = _inputs([("dso", dso_v), ("dio", dio_v), ("dpo", dpo_v)], sources)
    if dso_v is None or dio_v is None or dpo_v is None:
        return _fail(MISSING_INPUT, DAYS, "cash_conversion_cycle", inputs)
    return _ok(dso_v + dio_v - dpo_v, DAYS, "cash_conversion_cycle", inputs)


def accruals_ratio(
    net_income: Num, operating_cash_flow: Num, avg_total_assets: Num, *, sources: Sources = None
) -> MetricResult:
    inputs = _inputs(
        [
            ("net_income", net_income),
            ("operating_cash_flow", operating_cash_flow),
            ("avg_total_assets", avg_total_assets),
        ],
        sources,
    )
    numerator = (
        None
        if net_income is None or operating_cash_flow is None
        else net_income - operating_cash_flow
    )
    return _divide(
        numerator, avg_total_assets, unit=RATIO, formula_id="accruals_ratio", inputs=inputs
    )


@dataclass(frozen=True)
class OperatingBalances:
    """Year-end balances for net operating assets (balance-sheet accruals). None = not reported."""

    total_assets: Num = None
    cash_and_equivalents: Num = None
    short_term_investments: Num = None  # unreported in both years means none held (as net_cash)
    total_liabilities: Num = None
    total_debt: Num = None


def net_operating_assets(b: OperatingBalances) -> Num:
    """NOA = (total assets - cash - short-term investments) - (total liabilities - total debt)."""
    if (
        b.total_assets is None
        or b.cash_and_equivalents is None
        or b.total_liabilities is None
        or b.total_debt is None
    ):
        return None
    operating_assets = b.total_assets - b.cash_and_equivalents - (b.short_term_investments or 0)
    return operating_assets - (b.total_liabilities - b.total_debt)


def accruals_ratio_balance_sheet(
    current: OperatingBalances,
    prior: OperatingBalances,
    *,
    applicable: bool = True,
    sources: Sources = None,
) -> MetricResult:
    """Balance-sheet aggregate accruals: (NOA_t - NOA_t-1) / average NOA (ADR-0042)."""
    inputs = year_pair_inputs(current, prior, sources)
    if not applicable:
        return _fail(NOT_APPLICABLE_SECTOR, RATIO, "accruals_ratio_bs", inputs)
    noa, noa_prior = net_operating_assets(current), net_operating_assets(prior)
    # Securities reported in one year only is a tagging change, not a disposal: treating the
    # unreported year as zero would book the whole balance as an accrual.
    one_sided = (current.short_term_investments is None) != (prior.short_term_investments is None)
    if noa is None or noa_prior is None or one_sided:
        return _fail(MISSING_INPUT, RATIO, "accruals_ratio_bs", inputs)
    return _divide(
        noa - noa_prior,
        (noa + noa_prior) / 2,
        unit=RATIO,
        formula_id="accruals_ratio_bs",
        inputs=inputs,
    )


def sbc_pct_revenue(
    stock_based_comp: Num, revenue: Num, *, sources: Sources = None
) -> MetricResult:
    inputs = _inputs([("stock_based_comp", stock_based_comp), ("revenue", revenue)], sources)
    return _divide(
        stock_based_comp, revenue, unit=RATIO, formula_id="sbc_pct_revenue", inputs=inputs
    )


def dilution(current_shares: Num, prior_shares: Num, *, sources: Sources = None) -> MetricResult:
    inputs = _inputs([("current_shares", current_shares), ("prior_shares", prior_shares)], sources)
    numerator = (
        None if current_shares is None or prior_shares is None else current_shares - prior_shares
    )
    return _divide(numerator, prior_shares, unit=RATIO, formula_id="dilution", inputs=inputs)


def capex_intensity(capex: Num, revenue: Num, *, sources: Sources = None) -> MetricResult:
    inputs = _inputs([("capex", capex), ("revenue", revenue)], sources)
    return _divide(capex, revenue, unit=RATIO, formula_id="capex_intensity", inputs=inputs)


# --- Returns & quality ----------------------------------------------------------------
def roe(net_income: Num, shareholders_equity: Num, *, sources: Sources = None) -> MetricResult:
    inputs = _inputs(
        [("net_income", net_income), ("shareholders_equity", shareholders_equity)], sources
    )
    return _divide(net_income, shareholders_equity, unit=RATIO, formula_id="roe", inputs=inputs)


def roa(net_income: Num, total_assets: Num, *, sources: Sources = None) -> MetricResult:
    inputs = _inputs([("net_income", net_income), ("total_assets", total_assets)], sources)
    return _divide(net_income, total_assets, unit=RATIO, formula_id="roa", inputs=inputs)


def effective_tax_rate(
    income_tax_expense: Num, pretax_income: Num, *, sources: Sources = None
) -> MetricResult:
    """Income tax expense / pretax income; a pretax loss has no meaningful rate (NEGATIVE_BASE)."""
    inputs = _inputs(
        [("income_tax_expense", income_tax_expense), ("pretax_income", pretax_income)], sources
    )
    return _divide(
        income_tax_expense,
        pretax_income,
        unit=RATIO,
        formula_id="effective_tax_rate",
        inputs=inputs,
    )


def roic(
    operating_income: Num,
    tax_rate: Num,
    total_debt_value: Num,
    shareholders_equity: Num,
    cash_and_equivalents: Num,
    *,
    applicable: bool = True,
    sources: Sources = None,
) -> MetricResult:
    """ROIC = NOPAT / (debt + equity - cash); NOPAT = operating_income * (1 - tax_rate)."""
    inputs = _inputs(
        [
            ("operating_income", operating_income),
            ("tax_rate", tax_rate),
            ("total_debt", total_debt_value),
            ("shareholders_equity", shareholders_equity),
            ("cash_and_equivalents", cash_and_equivalents),
        ],
        sources,
    )
    if not applicable:
        return _fail(NOT_APPLICABLE_SECTOR, RATIO, "roic", inputs)
    if (
        operating_income is None
        or tax_rate is None
        or total_debt_value is None
        or shareholders_equity is None
        or cash_and_equivalents is None
    ):
        return _fail(MISSING_INPUT, RATIO, "roic", inputs)
    nopat = operating_income * (Decimal(1) - tax_rate)
    invested_capital = total_debt_value + shareholders_equity - cash_and_equivalents
    return _divide(nopat, invested_capital, unit=RATIO, formula_id="roic", inputs=inputs)


def year_pair_inputs(current: Any, prior: Any, sources: Sources) -> list[MetricInput]:
    """Lineage for a two-year score: every field of year t, then of t-1 as `prior_<field>`.

    `sources` uses the same keys, so each input links to its own year's source ID (DR-040).
    """
    pairs: list[tuple[str, Num]] = [(f.name, getattr(current, f.name)) for f in fields(current)]
    pairs += [(f"prior_{f.name}", getattr(prior, f.name)) for f in fields(prior)]
    return _inputs(pairs, sources)


def ratio_or_none(numerator: Num, denominator: Num) -> Num:
    """numerator / denominator, or None when either is missing or the denominator is zero."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _greater(left: Num, right: Num) -> bool | None:
    return None if left is None or right is None else left > right


@dataclass(frozen=True)
class YearFinancials:
    """One fiscal year's figures used by the Piotroski F-score. None = not reported."""

    net_income: Num = None
    operating_cash_flow: Num = None
    total_assets: Num = None
    long_term_debt: Num = None
    current_assets: Num = None
    current_liabilities: Num = None
    shares_diluted: Num = None  # the prior year's count is moved across any stock split first
    gross_profit: Num = None
    revenue: Num = None


PIOTROSKI_CRITERIA = (
    "positive_net_income",
    "positive_operating_cash_flow",
    "rising_roa",
    "cash_flow_exceeds_net_income",
    "falling_leverage",
    "rising_current_ratio",
    "no_share_dilution",
    "rising_gross_margin",
    "rising_asset_turnover",
)


def piotroski_criteria(current: YearFinancials, prior: YearFinancials) -> dict[str, bool | None]:
    """Each of Piotroski's (2000) nine binary tests; None = could not be computed (FR-EQ-007)."""
    c, p = current, prior
    zero = Decimal(0)
    shares = (
        None
        if c.shares_diluted is None or p.shares_diluted is None
        else c.shares_diluted <= p.shares_diluted
    )
    return dict(
        zip(
            PIOTROSKI_CRITERIA,
            (
                _greater(c.net_income, zero),
                _greater(c.operating_cash_flow, zero),
                _greater(
                    ratio_or_none(c.net_income, c.total_assets),
                    ratio_or_none(p.net_income, p.total_assets),
                ),
                _greater(c.operating_cash_flow, c.net_income),
                _greater(
                    ratio_or_none(p.long_term_debt, p.total_assets),
                    ratio_or_none(c.long_term_debt, c.total_assets),
                ),
                _greater(
                    ratio_or_none(c.current_assets, c.current_liabilities),
                    ratio_or_none(p.current_assets, p.current_liabilities),
                ),
                shares,
                _greater(
                    ratio_or_none(c.gross_profit, c.revenue),
                    ratio_or_none(p.gross_profit, p.revenue),
                ),
                _greater(
                    ratio_or_none(c.revenue, c.total_assets),
                    ratio_or_none(p.revenue, p.total_assets),
                ),
            ),
            strict=True,
        )
    )


def piotroski_f_score(
    current: YearFinancials,
    prior: YearFinancials,
    *,
    applicable: bool = True,
    sources: Sources = None,
) -> MetricResult:
    """9-point Piotroski F-score (not for financials). A total exists only when all nine tests
    can be computed; otherwise MISSING_INPUT, and the caller reports "n of 9 computable"."""
    inputs = year_pair_inputs(current, prior, sources)
    if not applicable:
        return _fail(NOT_APPLICABLE_SECTOR, SCORE, "piotroski_f_score", inputs)
    criteria = piotroski_criteria(current, prior)
    if any(passed is None for passed in criteria.values()):
        return _fail(MISSING_INPUT, SCORE, "piotroski_f_score", inputs)
    return _ok(
        Decimal(sum(1 for passed in criteria.values() if passed)),
        SCORE,
        "piotroski_f_score",
        inputs,
    )


# Altman (1968), J. Finance 23(4): original Z for public manufacturers. Z'' for non-manufacturers
# (book equity, no sales term): Altman et al. (2017), JIFMA 28(2). Zones per variant: (distress
# below, safe above); between is grey. Coefficients and cut-offs are quoted in ADR-0042.
AltmanVariant = Literal["original", "z_double_prime"]
ALTMAN_ZONES: dict[AltmanVariant, tuple[Decimal, Decimal]] = {
    "original": (Decimal("1.81"), Decimal("2.99")),
    "z_double_prime": (Decimal("1.10"), Decimal("2.60")),
}


def altman_zone(z: Decimal, variant: AltmanVariant) -> str:
    """distress | grey | safe, by the variant's own cut-offs."""
    distress, safe = ALTMAN_ZONES[variant]
    if z < distress:
        return "distress"
    return "safe" if z > safe else "grey"


def altman_z_score(
    working_capital: Num,
    retained_earnings: Num,
    ebit: Num,
    total_assets: Num,
    equity_value: Num,
    total_liabilities: Num,
    sales: Num = None,
    *,
    variant: AltmanVariant = "original",
    applicable: bool = True,
    sources: Sources = None,
) -> MetricResult:
    """Altman Z; N/A for financials. `variant` is chosen by the caller and never guessed here:
    "original" takes market value of equity and sales, "z_double_prime" book equity, no sales."""
    original = variant == "original"
    formula_id = "altman_z_score" if original else "altman_z_double_prime"
    named: list[tuple[str, Num]] = [
        ("working_capital", working_capital),
        ("retained_earnings", retained_earnings),
        ("ebit", ebit),
        ("total_assets", total_assets),
        ("market_value_equity" if original else "book_value_equity", equity_value),
        ("total_liabilities", total_liabilities),
    ]
    if original:
        named.append(("sales", sales))
    inputs = _inputs(named, sources)
    if not applicable:
        return _fail(NOT_APPLICABLE_SECTOR, SCORE, formula_id, inputs)
    if any(value is None for _, value in named):
        return _fail(MISSING_INPUT, SCORE, formula_id, inputs)
    wc, re_, eb, ta, eq, tl = (cast(Decimal, value) for _, value in named[:6])
    if ta <= 0 or tl <= 0:
        return _fail(NEGATIVE_BASE, SCORE, formula_id, inputs)
    if original:
        z = (
            Decimal("1.2") * (wc / ta)
            + Decimal("1.4") * (re_ / ta)
            + Decimal("3.3") * (eb / ta)
            + Decimal("0.6") * (eq / tl)
            + Decimal("1.0") * (cast(Decimal, sales) / ta)
        )
    else:
        z = (
            Decimal("6.56") * (wc / ta)
            + Decimal("3.26") * (re_ / ta)
            + Decimal("6.72") * (eb / ta)
            + Decimal("1.05") * (eq / tl)
        )
    return _ok(z, SCORE, formula_id, inputs)


def percentile_rank(value: Decimal, distribution: Sequence[Decimal]) -> MetricResult:
    """Fraction of the reference distribution at or below `value`, in [0, 1]."""
    inputs = [MetricInput(name="value", value=value)]
    if not distribution:
        return _fail(MISSING_INPUT, RATIO, "percentile_rank", inputs)
    at_or_below = sum(1 for d in distribution if d <= value)
    return _ok(Decimal(at_or_below) / Decimal(len(distribution)), RATIO, "percentile_rank", inputs)


# --- Footnotes (Phase 7f, FR-FN-006) --------------------------------------------------
DEBT_MATURITY_BUCKETS = (
    "debt_maturity_y1",
    "debt_maturity_y2",
    "debt_maturity_y3",
    "debt_maturity_y4",
    "debt_maturity_y5",
    "debt_maturity_after",
)


def lease_adjusted_debt(
    total_debt_value: Num, operating_lease_liability: Num, *, sources: Sources = None
) -> MetricResult:
    """Total debt + operating lease liabilities. Both are required: without the lease figure the
    result would just be total debt under a "lease-adjusted" label."""
    inputs = _inputs(
        [
            ("total_debt", total_debt_value),
            ("operating_lease_liability", operating_lease_liability),
        ],
        sources,
    )
    if total_debt_value is None or operating_lease_liability is None:
        return _fail(MISSING_INPUT, USD, "lease_adjusted_debt", inputs)
    return _ok(total_debt_value + operating_lease_liability, USD, "lease_adjusted_debt", inputs)


def debt_due_within_2y_share(
    maturities: Mapping[str, Num], *, sources: Sources = None
) -> MetricResult:
    """(year 1 + year 2 maturities) / all scheduled maturities; needs the whole ladder."""
    inputs = _inputs([(name, maturities.get(name)) for name in DEBT_MATURITY_BUCKETS], sources)
    values = [maturities.get(name) for name in DEBT_MATURITY_BUCKETS]
    if any(value is None for value in values):
        return _fail(MISSING_INPUT, RATIO, "debt_due_within_2y_share", inputs)
    y1, y2, *_ = (cast(Decimal, value) for value in values)
    total = sum((cast(Decimal, value) for value in values), Decimal(0))
    return _divide(y1 + y2, total, unit=RATIO, formula_id="debt_due_within_2y_share", inputs=inputs)


# --- Point-in-time valuation (Phase 7h, FR-PS-005, ADR-0046) ---------------------------
def pe_from_earnings(
    market_cap_value: Num, net_income_ttm: Num, *, sources: Sources = None
) -> MetricResult:
    """P/E as market cap / TTM net income: the same ratio as price / EPS without mixing share
    bases across a split. Loss-making (net income <= 0) is NEGATIVE_BASE, not a negative P/E."""
    inputs = _inputs(
        [("market_cap", market_cap_value), ("net_income_ttm", net_income_ttm)], sources
    )
    return _divide(
        market_cap_value, net_income_ttm, unit=MULTIPLE, formula_id="pe_ratio", inputs=inputs
    )


def ev_to_fcf(
    enterprise_value_v: Num, fcf_ttm: Num, *, applicable: bool = True, sources: Sources = None
) -> MetricResult:
    inputs = _inputs([("enterprise_value", enterprise_value_v), ("fcf_ttm", fcf_ttm)], sources)
    if not applicable:
        return _fail(NOT_APPLICABLE_SECTOR, MULTIPLE, "ev_fcf", inputs)
    return _divide(enterprise_value_v, fcf_ttm, unit=MULTIPLE, formula_id="ev_fcf", inputs=inputs)


def pb_ratio(
    market_cap_value: Num, book_equity: Num, *, applicable: bool = True, sources: Sources = None
) -> MetricResult:
    inputs = _inputs([("market_cap", market_cap_value), ("book_equity", book_equity)], sources)
    if not applicable:  # banks: price to tangible book, which needs a mapping we do not have
        return _fail(NOT_APPLICABLE_SECTOR, MULTIPLE, "pb_ratio", inputs)
    return _divide(
        market_cap_value, book_equity, unit=MULTIPLE, formula_id="pb_ratio", inputs=inputs
    )


def earnings_yield(
    net_income_ttm: Num, market_cap_value: Num, *, sources: Sources = None
) -> MetricResult:
    """TTM net income / market cap; a loss gives a negative yield, which is meaningful."""
    inputs = _inputs(
        [("net_income_ttm", net_income_ttm), ("market_cap", market_cap_value)], sources
    )
    return _divide(
        net_income_ttm, market_cap_value, unit=RATIO, formula_id="earnings_yield", inputs=inputs
    )
