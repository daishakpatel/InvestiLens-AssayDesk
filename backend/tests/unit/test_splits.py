"""Per-share history on today's share basis (DR-026, ADR-0051): NVIDIA's 2021 4:1 and 2024 10:1."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.finance.splits import filed_dates, merge_splits, restate

_PRICED = [(date(2024, 6, 10), Decimal(10))]  # inside the price feed's five-year window
_TAGGED = [  # NVIDIA's StockSplitConversionRatio tags, dated by their contexts
    (date(2021, 6, 3), Decimal(4)),
    (date(2021, 7, 19), Decimal(4)),
    (date(2024, 5, 31), Decimal(10)),
    (date(2024, 6, 30), Decimal(10)),
]
_SPLITS = merge_splits(_PRICED, _TAGGED)


def test_merge_keeps_one_entry_per_split_preferring_the_price_feed_ex_date() -> None:
    splits = merge_splits(_PRICED, _TAGGED)
    assert splits == [(date(2021, 7, 19), Decimal(4)), (date(2024, 6, 10), Decimal(10))]


def test_merge_ignores_ratios_that_are_not_splits() -> None:
    assert merge_splits([], [(date(2020, 1, 1), Decimal(1)), (date(2020, 1, 1), Decimal(0))]) == []


def test_a_figure_filed_before_a_split_is_restated_with_a_note() -> None:
    fy2024 = restate(Decimal("11.93"), "USD/shares", date(2024, 2, 21), _SPLITS)
    assert fy2024.value == Decimal("1.193")
    assert fy2024.note == (
        "Split-adjusted: as filed 11.93, divided by 10 for later stock splits "
        "(10-for-1 on 2024-06-10)"
    )
    fy2021 = restate(Decimal("6.90"), "USD/shares", date(2021, 2, 26), _SPLITS)
    assert fy2021.value == Decimal("0.1725")  # both splits: ÷ 40


def test_share_counts_are_multiplied_not_divided() -> None:
    shares = restate(Decimal(2_494_000_000), "shares", date(2024, 2, 21), _SPLITS)
    assert shares.value == Decimal(24_940_000_000)
    assert shares.note is not None and "multiplied by 10" in shares.note


def test_figures_already_on_todays_basis_pass_through() -> None:
    after = restate(Decimal("2.94"), "USD/shares", date(2025, 2, 26), _SPLITS)
    assert after.value == Decimal("2.94") and after.note is None
    money = restate(Decimal(60_922_000_000), "USD", date(2024, 2, 21), _SPLITS)
    assert money.value == Decimal(60_922_000_000) and money.note is None
    unknown = restate(Decimal("11.93"), "USD/shares", None, _SPLITS)
    assert unknown.value == Decimal("11.93") and unknown.note is None


def test_a_reverse_split_raises_per_share_history() -> None:
    reverse = [(date(2023, 1, 1), Decimal("0.1"))]  # 1-for-10
    restated = restate(Decimal("0.50"), "USD/shares", date(2022, 3, 1), reverse)
    assert restated.value == Decimal(5)
    assert restated.note is not None and "1-for-10" in restated.note


def test_no_accessions_returns_nothing_without_querying() -> None:
    # An unbound session raises on any query, so this also proves the database is not touched.
    assert filed_dates(Session(), 1, []) == {}
