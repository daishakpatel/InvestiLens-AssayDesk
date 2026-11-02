"""Stock-price questions and plain "margins" (ADR-0054; RAG-002, RAG-030)."""

from __future__ import annotations

from datetime import date

import pytest

from app.rag.intent import classify_intent
from app.rag.pipeline import _extract_metrics
from app.rag.prices import is_price_question, price_day
from app.rag.types import Intent

LATEST = date(2026, 10, 29)
TODAY = date(2026, 11, 1)


@pytest.mark.parametrize(
    "question",
    [
        "what was the stock high on Sep 29",
        "stock price today",
        "What did it close at on October 4?",
        "What's the share price?",
        "What is NVDA trading at?",
        "What was the high price for the stock last week?",
    ],
)
def test_price_questions_are_recognised(question: str) -> None:
    assert is_price_question(question)


@pytest.mark.parametrize(
    "question",
    [
        "Why is stock-based compensation so high?",
        "What was the average price paid per share for buybacks?",
        "Is market share high in data center?",
        "What is the price-to-earnings ratio?",
        "How many stock options were granted at a low exercise price?",
        "What were stockholders' equity and the closing balance?",
    ],
)
def test_other_price_wording_is_not_a_price_question(question: str) -> None:
    assert not is_price_question(question)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("stock price today", LATEST),  # no date: the latest stored bar
        ("close on 2026-03-02", date(2026, 3, 2)),
        ("the stock high on Sep 29", date(2026, 9, 29)),
        ("the stock high on 29th of September", date(2026, 9, 29)),
        ("close on Dec 15", date(2025, 12, 15)),  # after the latest bar: last year's
        ("close on Dec 15, 2024", date(2024, 12, 15)),
        ("close on 10/2", date(2026, 10, 2)),
        ("stock price yesterday", date(2026, 10, 31)),
    ],
)
def test_the_asked_day_is_resolved(question: str, expected: date) -> None:
    assert price_day(question, latest=LATEST, today=TODAY) == expected


def test_an_impossible_date_raises_so_the_answer_abstains() -> None:
    with pytest.raises(ValueError):
        price_day("stock price on Feb 30", latest=LATEST, today=TODAY)


def test_plain_margins_ask_for_all_three() -> None:
    assert classify_intent("what were margins in FY2025?").intent == Intent.FINANCIAL_METRIC
    assert _extract_metrics("how did margins trend?") == (
        "gross_margin",
        "operating_margin",
        "net_margin",
    )
    assert _extract_metrics("gross margin in FY2025") == ("gross_margin",)
    # Buyback prices are in the filings, not financial_metrics.
    assert classify_intent("What was the average price paid per share?").intent != (
        Intent.FINANCIAL_METRIC
    )


def test_commentary_about_a_metric_is_not_a_lookup() -> None:  # RAG-002
    questions = (
        "What did management say about NVIDIA's margins?",
        "What did they say about revenue?",
    )
    for q in questions:
        assert classify_intent(q).intent != Intent.FINANCIAL_METRIC
