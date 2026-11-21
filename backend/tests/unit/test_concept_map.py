"""Concept map, selection, fiscal calendar, Q4, restatement, and validation tests (Phase 1b).

Offline: driven by the frozen companyfacts fixtures and golden_metrics.json. Covers DR-020..028.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.finance.concept_map import load_concept_map
from app.finance.derivation import decumulate, derive_q4
from app.finance.facts import fact_views
from app.finance.periods import fiscal_year_of, is_full_year, is_quarter
from app.finance.selection import FactView, rank_restatements, select_metric
from app.finance.validation import check_accounting_identity, check_unit
from app.ingestion.sec.xbrl import parse_companyfacts
from app.providers.sec.mock import MockSecSource

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_GOLDEN = {
    c["ticker"]: c for c in json.loads((_FIXTURES / "golden_metrics.json").read_text())["companies"]
}
_CIK = {"NVDA": "0001045810", "AAPL": "0000320193", "JPM": "0000019617"}


def _facts(ticker: str) -> list[FactView]:
    rows, _ = parse_companyfacts(1, MockSecSource().companyfacts(_CIK[ticker]))
    return fact_views(rows)


def _latest_fye(facts: list[FactView], revenue_tags: list[str], fye_month: int) -> date:
    return max(
        f.period_end
        for f in facts
        if f.concept_tag in revenue_tags
        and f.period_end
        and f.period_end.month == fye_month
        and is_full_year(f.period_start, f.period_end)
    )


def _fye_month(facts: list[FactView], revenue_tags: list[str]) -> int:
    from collections import Counter

    months = [
        f.period_end.month
        for f in facts
        if f.concept_tag in revenue_tags
        and f.period_end
        and is_full_year(f.period_start, f.period_end)
    ]
    return Counter(months).most_common(1)[0][0]


# --- DR-020: concept selection matches golden (DoD #1, #2) ---------------------------
def test_selection_matches_golden_for_all_seed_companies() -> None:
    for ticker in ("NVDA", "AAPL", "JPM"):
        facts = _facts(ticker)
        cmap = load_concept_map(ticker=ticker, cik=_CIK[ticker])
        fye_month = _fye_month(facts, cmap.tags("revenue"))
        fye = _latest_fye(facts, cmap.tags("revenue"), fye_month)
        golden = _GOLDEN[ticker]["metrics"]
        for metric in ("revenue", "net_income", "eps_diluted"):
            selection = select_metric(facts, cmap, metric, period_end=fye)
            assert selection is not None, f"{ticker} {metric} not selected"
            assert str(selection.value) == golden[metric]["value"], f"{ticker} {metric}"


def test_jpm_override_marks_bank_and_resolves_revenue() -> None:
    cmap = load_concept_map(ticker="JPM", cik=_CIK["JPM"])
    assert cmap.sector == "bank"
    assert cmap.applies("gross_margin") is False  # DR-001 sector applicability
    assert cmap.applies("pe") is True
    assert cmap.tags("revenue")[0] == "us-gaap:Revenues"


def test_amd_pretax_income_uses_its_statement_line_first() -> None:
    """AMD's 10-Qs tag pretax income only with the equity-method variant (FR-QT-010)."""
    tags = load_concept_map(ticker="AMD").tags("pretax_income")
    assert tags[0].endswith("MinorityInterestAndIncomeLossFromEquityMethodInvestments")
    assert load_concept_map(ticker="NVDA").tags("pretax_income") == tags[1:]


# --- DR-021: NVIDIA non-calendar fiscal year end (DoD #3) ----------------------------
def test_nvidia_fiscal_year_end_is_last_sunday_of_january() -> None:
    facts = _facts("NVDA")
    revenue_tags = load_concept_map(ticker="NVDA").tags("revenue")
    fye_month = _fye_month(facts, revenue_tags)
    assert fye_month == 1  # January
    fye = _latest_fye(facts, revenue_tags, fye_month)
    assert fye.month == 1 and fye.weekday() == 6  # Sunday (weekday 6)
    assert fiscal_year_of(fye, fye_month) == fye.year


# --- DR-022: Q4 derivation + lineage (DoD #4) ----------------------------------------
def test_nvidia_q4_revenue_is_derived_and_consistent() -> None:
    facts = _facts("NVDA")
    cmap = load_concept_map(ticker="NVDA")
    fye_month = _fye_month(facts, cmap.tags("revenue"))
    fye = _latest_fye(facts, cmap.tags("revenue"), fye_month)
    fiscal_year = fiscal_year_of(fye, fye_month)
    quarter_ends = sorted(
        {
            f.period_end
            for f in facts
            if f.concept_tag in cmap.tags("revenue")
            and f.period_end
            and fiscal_year_of(f.period_end, fye_month) == fiscal_year
            and is_quarter(f.period_start, f.period_end)
        }
    )[:3]
    assert len(quarter_ends) == 3
    fy = select_metric(facts, cmap, "revenue", period_end=fye)
    q = [select_metric(facts, cmap, "revenue", period_end=qe, quarter=True) for qe in quarter_ends]
    assert fy and all(q)
    result = derive_q4(fy, q[0], q[1], q[2])  # type: ignore[arg-type]
    assert len(result.input_accessions) == 4  # lineage: FY + Q1 + Q2 + Q3 (CIT-003)
    # FY == Q1 + Q2 + Q3 + Q4 (within exact integer arithmetic here)
    assert fy.value == q[0].value + q[1].value + q[2].value + result.value  # type: ignore[union-attr]
    assert result.value > 0


def test_decumulate_turns_ytd_into_quarters() -> None:
    assert decumulate([Decimal(10), Decimal(25), Decimal(45), Decimal(70)]) == [
        Decimal(10),
        Decimal(15),
        Decimal(20),
        Decimal(25),
    ]


# --- DR-023: restatement -> two rows, one is_latest (DoD #5) --------------------------
def test_restatement_keeps_two_rows_one_latest() -> None:
    original = FactView(
        "us-gaap:NetIncomeLoss",
        date(2024, 1, 1),
        date(2024, 12, 31),
        "duration",
        Decimal("100"),
        "USD",
        "acc-orig",
        date(2025, 2, 1),
        False,
    )
    amended = FactView(
        "us-gaap:NetIncomeLoss",
        date(2024, 1, 1),
        date(2024, 12, 31),
        "duration",
        Decimal("110"),
        "USD",
        "acc-amend",
        date(2025, 6, 1),
        True,
    )
    ranked = rank_restatements([original, amended])
    assert len(ranked) == 2
    latest = [f for f, is_latest in ranked if is_latest]
    assert len(latest) == 1 and latest[0].accession_number == "acc-amend"


def test_later_comparative_does_not_override_original() -> None:
    """A later non-amendment (e.g. 10-Q comparative) must not supersede the as-reported 10-K."""
    original = FactView(
        "us-gaap:Revenues",
        date(2025, 1, 1),
        date(2025, 12, 31),
        "duration",
        Decimal("182448"),
        "USD",
        "10k",
        date(2026, 2, 13),
        False,
    )
    comparative = FactView(
        "us-gaap:Revenues",
        date(2025, 1, 1),
        date(2025, 12, 31),
        "duration",
        Decimal("182000"),
        "USD",
        "10q",
        date(2026, 4, 6),
        False,
    )
    latest = [f for f, is_latest in rank_restatements([original, comparative]) if is_latest]
    assert latest[0].accession_number == "10k"


# --- DR-024 / DR-028: validation (DoD #6) --------------------------------------------
def test_inconsistent_unit_is_flagged() -> None:
    # eps_diluted expects per_share (USD/shares); a currency unit must be flagged.
    issue = check_unit("eps_diluted", "USD", "per_share")
    assert issue is not None and issue.issue_code == "UNIT_MISMATCH"
    assert check_unit("eps_diluted", "USD/shares", "per_share") is None


def test_accounting_identity_flags_imbalance() -> None:
    assert check_accounting_identity(Decimal(100), Decimal(40), Decimal(60)) is None
    issue = check_accounting_identity(Decimal(100), Decimal(40), Decimal(30))
    assert issue is not None and issue.issue_code == "ACCOUNTING_IDENTITY"
