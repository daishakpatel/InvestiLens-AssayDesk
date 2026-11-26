"""Pure helpers behind the valuation builder (DR-025/026: shares and prices across splits)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.finance.valuation_builder import (
    PricePoint,
    ShareCount,
    count_as_of,
    price_on_or_before,
    share_count_for,
    shares_on,
    split_factor,
)

_SPLITS = [(date(2024, 6, 10), Decimal(10)), (date(2021, 7, 20), Decimal(4))]


def test_split_factor_counts_only_splits_inside_the_window() -> None:
    assert split_factor(_SPLITS, date(2024, 1, 1), date(2024, 12, 31)) == 10
    assert split_factor(_SPLITS, date(2021, 1, 1), date(2024, 12, 31)) == 40
    assert split_factor(_SPLITS, date(2024, 6, 10), date(2024, 12, 31)) == 1  # ex-date excluded
    assert split_factor([], date(2020, 1, 1), date(2026, 1, 1)) == 1


def test_share_count_moves_to_the_price_date_across_a_split() -> None:  # DR-026
    before = ShareCount(date(2024, 2, 16), Decimal("2_464_000_000"), "xbrl:1")
    after = ShareCount(date(2024, 8, 23), Decimal("24_530_000_000"), "xbrl:2")
    # A count recorded before the split, priced after it, is ten times larger.
    assert shares_on(before, date(2024, 9, 30), _SPLITS) == Decimal("24_640_000_000")
    # A count recorded after the split, priced before it, is ten times smaller.
    assert shares_on(after, date(2024, 1, 26), _SPLITS) == Decimal("2_453_000_000")
    assert shares_on(after, date(2024, 9, 30), _SPLITS) == after.shares


def test_share_count_for_a_year_end_prefers_the_next_cover_page() -> None:
    early = ShareCount(date(2025, 8, 1), Decimal(1), "xbrl:1")
    cover = ShareCount(date(2026, 2, 13), Decimal(2), "xbrl:2")
    later = ShareCount(date(2026, 5, 15), Decimal(3), "xbrl:3")
    assert share_count_for([early, cover, later], date(2026, 1, 25)) == cover
    assert share_count_for([early], date(2026, 1, 25)) == early  # nothing filed after it yet
    assert share_count_for([], date(2026, 1, 25)) is None


def test_a_share_count_over_a_year_old_is_not_used() -> None:  # DR-025, ADR-0074
    # Visa's and Mastercard's only un-dimensioned counts are from 2010: a 2026 market cap built on
    # them was a fifth of the real one.
    old = ShareCount(date(2010, 1, 27), Decimal(1), "xbrl:1")
    recent = ShareCount(date(2025, 11, 1), Decimal(2), "xbrl:2")
    assert count_as_of([old], date(2026, 11, 25)) is None
    assert count_as_of([old, recent], date(2026, 11, 1)) == recent  # exactly 365 days old
    assert share_count_for([old], date(2025, 9, 30)) is None
    assert share_count_for([old], date(2010, 9, 30)) == old


def test_price_on_or_before_takes_the_last_trading_day() -> None:
    prices = [PricePoint(date(2026, 1, 22), Decimal(1)), PricePoint(date(2026, 1, 23), Decimal(2))]
    assert price_on_or_before(prices, date(2026, 1, 25)) == prices[1]  # a Sunday year end
    assert price_on_or_before(prices, date(2026, 1, 22)) == prices[0]
    assert price_on_or_before(prices, date(2026, 1, 1)) is None
