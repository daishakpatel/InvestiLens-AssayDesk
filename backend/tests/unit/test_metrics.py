"""Deterministic metric formula tests (Phase 1c, spec §12). Offline.

Covers: golden-fixture exact match (NVDA/AAPL/JPM), sector applicability (DR-042), reason codes
(DR-041), lineage inputs (DR-040), and property-based invariants (Hypothesis). Targets 100%
coverage of `app/finance/metrics.py`.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

import app.finance.metrics as m
from app.finance.concept_map import ConceptMap, load_concept_map
from app.finance.facts import fact_views
from app.finance.periods import is_full_year
from app.finance.selection import Selection, select_metric
from app.ingestion.sec.xbrl import parse_companyfacts
from app.providers.sec.mock import MockSecSource

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_GOLDEN = {
    c["ticker"]: c for c in json.loads((_FIXTURES / "golden_metrics.json").read_text())["companies"]
}
_CIK = {"NVDA": "0001045810", "AAPL": "0000320193", "JPM": "0000019617"}
_D = Decimal


def _latest_annual_values(ticker: str) -> tuple[ConceptMap, dict[str, Selection | None]]:
    from collections import Counter

    rows, _ = parse_companyfacts(1, MockSecSource().companyfacts(_CIK[ticker]))
    facts = fact_views(rows)
    cmap = load_concept_map(ticker=ticker, cik=_CIK[ticker])
    revenue_tags = cmap.tags("revenue")
    annual_ends = [
        f.period_end
        for f in facts
        if f.concept_tag in revenue_tags
        and f.period_end
        and is_full_year(f.period_start, f.period_end)
    ]
    fye_month = Counter(e.month for e in annual_ends).most_common(1)[0][0]
    fye = max(e for e in annual_ends if e.month == fye_month)
    selected = {
        name: select_metric(facts, cmap, name, period_end=fye)
        for name in ("revenue", "gross_profit", "cost_of_revenue", "net_income")
    }
    return cmap, selected


# --- Golden exact match (hard gate, DoD #3) -------------------------------------------
def test_gross_margin_matches_golden_for_nvda_and_aapl() -> None:
    for ticker in ("NVDA", "AAPL"):
        cmap, sel = _latest_annual_values(ticker)
        assert sel["revenue"] is not None
        gp = sel["gross_profit"].value if sel["gross_profit"] else None
        cost = sel["cost_of_revenue"].value if sel["cost_of_revenue"] else None
        result = m.gross_margin(
            gp, sel["revenue"].value, cost, applicable=cmap.applies("gross_margin")
        )
        assert result.value is not None
        golden = _GOLDEN[ticker]["metrics"]["gross_margin"]["value"]
        assert result.value.quantize(_D("0.0001")) == _D(golden)


def test_jpm_gross_margin_is_not_applicable() -> None:
    cmap, sel = _latest_annual_values("JPM")
    assert sel["revenue"] is not None
    result = m.gross_margin(
        None, sel["revenue"].value, None, applicable=cmap.applies("gross_margin")
    )
    assert result.value is None
    assert result.warnings == [m.NOT_APPLICABLE_SECTOR]
    assert _GOLDEN["JPM"]["metrics"]["gross_margin"]["value"] is None


def test_bank_inapplicable_metrics_resolve_na_not_zero() -> None:
    cmap = load_concept_map(ticker="JPM", cik=_CIK["JPM"])
    assert m.current_ratio(_D(10), _D(5), applicable=cmap.applies("current_ratio")).value is None
    assert (
        m.quick_ratio(_D(10), _D(1), _D(1), _D(5), applicable=cmap.applies("current_ratio")).value
        is None
    )
    assert m.ev_to_ebitda(_D(10), _D(5), applicable=cmap.applies("ev_ebitda")).value is None
    na = m.ev_to_revenue(_D(10), _D(5), applicable=cmap.applies("ev_ebitda"))
    assert na.value is None and na.warnings == [m.NOT_APPLICABLE_SECTOR]  # DR-042
    assert m.altman_z_score(
        _D(1), _D(1), _D(1), _D(1), _D(1), _D(1), _D(1), applicable=cmap.applies("altman")
    ).warnings == [m.NOT_APPLICABLE_SECTOR]


# --- Lineage (DR-040, DoD #5) ---------------------------------------------------------
def test_metric_result_carries_traceable_inputs() -> None:
    sources = {"gross_profit": "xbrl_gp_FY2025", "revenue": "xbrl_rev_FY2025"}
    result = m.gross_margin(_D(70), _D(100), sources=sources)
    names = {i.name: i.source_id for i in result.inputs}
    assert names["gross_profit"] == "xbrl_gp_FY2025"
    assert names["revenue"] == "xbrl_rev_FY2025"
    assert result.formula_id == "gross_margin" and result.formula_version == m.FORMULA_VERSION


# --- Reason codes (DR-041) ------------------------------------------------------------
def test_missing_input_returns_none_with_reason() -> None:
    assert m.net_margin(None, _D(100)).warnings == [m.MISSING_INPUT]
    assert m.ebitda(_D(10), None).warnings == [m.MISSING_INPUT]
    assert m.free_cash_flow(_D(10), None).warnings == [m.MISSING_INPUT]
    assert m.total_debt(None, None, None, None).warnings == [m.MISSING_INPUT]
    assert m.net_cash(None, _D(1), _D(1)).warnings == [m.MISSING_INPUT]
    assert m.roic(None, _D("0.2"), _D(1), _D(1), _D(1)).warnings == [m.MISSING_INPUT]
    assert m.ttm_sum([_D(1), _D(1), _D(1)]).warnings == [m.MISSING_INPUT]
    assert m.ttm_sum([_D(1), _D(1), _D(1), None]).warnings == [m.MISSING_INPUT]
    assert m.cash_conversion_cycle(_D(1), None, _D(1)).warnings == [m.MISSING_INPUT]
    assert m.percentile_rank(_D(1), []).warnings == [m.MISSING_INPUT]
    assert m.enterprise_value(None, _D(1), _D(1)).warnings == [m.MISSING_INPUT]
    assert m.quick_ratio(None, _D(1), _D(1), _D(1)).warnings == [m.MISSING_INPUT]
    assert m.cash_to_debt(None, _D(1), _D(1)).warnings == [m.MISSING_INPUT]
    assert m.altman_z_score(None, _D(1), _D(1), _D(1), _D(1), _D(1), _D(1)).warnings == [
        m.MISSING_INPUT
    ]


def test_zero_or_negative_base_returns_reason() -> None:
    assert m.net_margin(_D(10), _D(0)).warnings == [m.NEGATIVE_BASE]
    assert m.net_margin(_D(10), _D(-5)).warnings == [m.NEGATIVE_BASE]
    assert m.fcf_conversion(_D(10), _D(0)).warnings == [m.NEGATIVE_BASE]
    assert m.pe_ratio(_D(100), _D(0)).warnings == [m.NEGATIVE_BASE]
    assert m.cagr(_D(-1), _D(10), 5).warnings == [m.NEGATIVE_BASE]
    assert m.cagr(_D(10), _D(10), 0).warnings == [m.NEGATIVE_BASE]
    assert m.cagr(None, _D(10), 5).warnings == [m.MISSING_INPUT]
    assert m.roic(_D(10), _D("0.2"), _D(0), _D(0), _D(0)).warnings == [m.NEGATIVE_BASE]
    assert m.altman_z_score(_D(1), _D(1), _D(1), _D(0), _D(1), _D(1), _D(1)).warnings == [
        m.NEGATIVE_BASE
    ]


# --- Happy paths covering remaining branches ------------------------------------------
def test_core_formulas_compute() -> None:
    assert m.yoy_growth(_D(110), _D(100)).value == _D("0.1")
    assert m.operating_margin(_D(20), _D(100)).value == _D("0.2")
    assert m.ebitda(_D(20), _D(5)).value == _D(25)
    assert m.free_cash_flow(_D(30), _D(10)).value == _D(20)
    assert m.fcf_margin(_D(20), _D(100)).value == _D("0.2")
    assert m.fcf_conversion(_D(20), _D(40)).value == _D("0.5")
    assert m.total_debt(_D(10), _D(90), _D(5), _D(5)).value == _D(110)
    assert m.net_cash(_D(100), None, _D(40)).value == _D(60)  # sti None -> 0
    assert m.net_cash(_D(100), _D(20), _D(40)).value == _D(80)
    assert m.cash_to_debt(_D(100), None, _D(50)).value == _D(2)
    assert m.current_ratio(_D(200), _D(100)).value == _D(2)
    assert m.quick_ratio(_D(100), None, None, _D(50)).value == _D(2)
    assert m.quick_ratio(_D(50), _D(30), _D(20), _D(50)).value == _D(2)
    assert m.debt_to_equity(_D(50), _D(100)).value == _D("0.5")
    assert m.market_cap(_D("10"), _D(1000)).value == _D(10000)
    assert m.market_cap(None, _D(1000)).warnings == [m.MISSING_INPUT]
    assert m.enterprise_value(_D(10000), _D(500), _D(300), _D(200), _D(0), _D(0)).value == _D(10000)
    assert m.ps_ratio(_D(1000), _D(250)).value == _D(4)
    assert m.p_fcf(_D(1000), _D(100)).value == _D(10)
    assert m.ev_to_revenue(_D(1000), _D(250)).value == _D(4)
    assert m.ev_to_ebitda(_D(1000), _D(100)).value == _D(10)
    assert m.fcf_yield(_D(100), _D(1000)).value == _D("0.1")
    assert m.dso(_D(50), _D(365), days=365).value == _D(50)
    assert m.dio(_D(50), _D(365), days=365).value == _D(50)
    assert m.dpo(_D(50), _D(365), days=365).value == _D(50)
    assert m.dso(None, _D(365)).warnings == [m.MISSING_INPUT]
    assert m.cash_conversion_cycle(_D(50), _D(40), _D(30)).value == _D(60)
    assert m.accruals_ratio(_D(10), _D(30), _D(100)).value == _D("-0.2")
    assert m.sbc_pct_revenue(_D(5), _D(100)).value == _D("0.05")
    assert m.dilution(_D(110), _D(100)).value == _D("0.1")
    assert m.capex_intensity(_D(10), _D(100)).value == _D("0.1")
    assert m.roe(_D(20), _D(100)).value == _D("0.2")
    assert m.roa(_D(20), _D(200)).value == _D("0.1")
    assert m.roic(_D(100), _D("0.2"), _D(50), _D(150), _D(50)).value == _D(80) / _D(150)
    assert m.ttm_sum([_D(1), _D(2), _D(3), _D(4)]).value == _D(10)
    assert m.percentile_rank(_D(5), [_D(1), _D(5), _D(10)]).value == _D(2) / _D(3)
    assert m.gross_margin(None, _D(100), _D(40)).value == _D("0.6")  # from revenue - cost


def test_piotroski_counts_passed_tests() -> None:
    strong = m.YearFinancials(
        net_income=_D(100),
        operating_cash_flow=_D(120),
        total_assets=_D(1000),
        long_term_debt=_D(100),
        current_assets=_D(500),
        current_liabilities=_D(200),
        shares_diluted=_D(10),
        gross_profit=_D(400),
        revenue=_D(800),
    )
    weak = m.YearFinancials(
        net_income=_D(50),
        operating_cash_flow=_D(40),
        total_assets=_D(1000),
        long_term_debt=_D(200),
        current_assets=_D(300),
        current_liabilities=_D(200),
        shares_diluted=_D(10),
        gross_profit=_D(300),
        revenue=_D(700),
    )
    score = m.piotroski_f_score(strong, weak)
    assert score.unit == m.SCORE and 0 <= score.value <= 9  # type: ignore[operator]
    assert m.piotroski_f_score(strong, weak, applicable=False).warnings == [m.NOT_APPLICABLE_SECTOR]


def test_altman_and_percentile_compute() -> None:
    z = m.altman_z_score(_D(200), _D(300), _D(150), _D(1000), _D(2000), _D(500), _D(900))
    assert z.value is not None and z.unit == m.SCORE
    assert m.enterprise_value(_D(1), _D(1), _D(1), applicable=False).warnings == [
        m.NOT_APPLICABLE_SECTOR
    ]


# --- Property-based invariants (DoD #7) -----------------------------------------------
_amounts = st.decimals(
    min_value=1, max_value=10**9, places=2, allow_nan=False, allow_infinity=False
)


@given(_amounts)
def test_cagr_of_flat_series_is_zero(value: Decimal) -> None:
    result = m.cagr(value, value, 5)
    assert result.value is not None and result.value == _D(0)


@given(_amounts, st.integers(min_value=1, max_value=20))
def test_cagr_of_growing_series_is_positive(start: Decimal, years: int) -> None:
    result = m.cagr(start, start * 2, years)
    assert result.value is not None and result.value > 0


@given(_amounts)
def test_yoy_of_equal_values_is_zero(value: Decimal) -> None:
    assert m.yoy_growth(value, value).value == _D(0)


@given(st.decimals(min_value=-(10**6), max_value=0, places=2, allow_nan=False))
def test_division_never_raises_on_bad_base(denominator: Decimal) -> None:
    result = m.net_margin(_D(10), denominator)  # 0 or negative base
    assert result.value is None and result.warnings == [m.NEGATIVE_BASE]


@given(_amounts, _amounts)
def test_margin_is_ratio_of_inputs(numerator: Decimal, denominator: Decimal) -> None:
    result = m.net_margin(numerator, denominator)
    assert result.value == numerator / denominator


def test_average_uses_valued_years_and_needs_three() -> None:  # spec §10.7, ADR-0069
    loss_year = None  # a P/E is N/A in a loss year, never zero
    result = m.average([Decimal("10"), Decimal("20"), loss_year, Decimal("60")], unit=m.MULTIPLE)
    assert result.value == Decimal("30") and result.unit == m.MULTIPLE
    assert m.average([Decimal("10"), loss_year, Decimal("20")], unit=m.MULTIPLE).value is None
