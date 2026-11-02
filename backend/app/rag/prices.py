"""Stock-price questions (ADR-0054, RAG-030).

"What was the stock high on Sep 29?" or "stock price today" is answered from `price_history`, never
from document search: filings hold no daily prices, and the search sent a whole context budget of
unrelated passages. Detection and date parsing are pure functions so they are tested offline.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

# A question about the stock's trading price. "stock" excludes stock-based compensation, stock
# options and stockholders; bare "share" is left out ("market share is high").
_STOCK = r"\bstock\b(?![- ]?(based|holders?|options?|awards?|comp))"
_PRICE_WORD = r"\b(price|high|low|close[ds]?|closing|open(ed|ing)?|trad(ed|ing)|quote)\b"
_PRICE_QUESTION = re.compile(
    rf"{_STOCK}[^.?!]*{_PRICE_WORD}|\b(price|high|low|close[ds]?|closing)\b[^.?!]*{_STOCK}|"
    r"\bshare price\b|\bclos(e|ed|ing) (at|price)\b|\btrad(es|ed|ing) at\b|\btrading volume\b|"
    r"\b(current|latest|today'?s) price\b|\bprice (today|now|yesterday)\b",
    re.IGNORECASE,
)
# Price wording that is about something else: per-share figures, multiples, buybacks, options.
_NOT_PRICE = re.compile(
    r"\b(per share|price[- ]to|p/?e ratio|repurchases?|buybacks?|exercise price|strike price|"
    r"offering price|price paid|purchase price)\b",
    re.IGNORECASE,
)

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
    )
}
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_DAY = re.compile(rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.I)
_DAY_MONTH = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}(?:,?\s+(\d{{4}}))?", re.I
)
_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b")  # US month/day[/year]
_YESTERDAY = re.compile(r"\byesterday\b", re.IGNORECASE)


def is_price_question(question: str) -> bool:
    return bool(_PRICE_QUESTION.search(question)) and not _NOT_PRICE.search(question)


def price_day(question: str, *, latest: date, today: date) -> date:
    """The trading day asked about; the latest stored bar when no date is given.

    A date without a year is its most recent occurrence on or before `latest`. Raises ValueError
    for an impossible date ("Feb 30"), so the caller abstains instead of answering another day.
    """
    if m := _ISO.search(question):
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if _YESTERDAY.search(question):
        return today - timedelta(days=1)
    month = day = year = None
    if m := _MONTH_DAY.search(question):
        month, day, year = _MONTHS[m.group(1).lower()[:3]], int(m.group(2)), m.group(3)
    elif m := _DAY_MONTH.search(question):
        month, day, year = _MONTHS[m.group(2).lower()[:3]], int(m.group(1)), m.group(3)
    elif m := _NUMERIC.search(question):
        month, day, year = int(m.group(1)), int(m.group(2)), m.group(3)
    if month is None or day is None:
        return latest
    if year:
        return date(int(year), month, day)
    candidate = date(latest.year, month, day)
    return candidate if candidate <= latest else date(latest.year - 1, month, day)
