"""Query rewriting & decomposition (RAG-003).

Three deterministic transforms (a cheap-model step in RAG-050; rules here, LLM later):
- expand financial synonyms/abbreviations so keyword search hits exact terms,
- resolve relative time references ("last three years") to actual fiscal years using the
  company's `fiscal_calendars` (passed in, so this stays a pure function),
- decompose explanation/multi-part questions into sub-queries for broader retrieval.
"""

from __future__ import annotations

import re

from app.rag.types import Intent, RewrittenQuery

# Bidirectional financial synonyms (§16.3). Expansion appends the alias so both forms are indexed.
_SYNONYMS: dict[str, str] = {
    "gross margin": "gross profit percentage",
    "operating margin": "operating income percentage",
    "net margin": "net profit margin",
    "fcf": "free cash flow",
    "r&d": "research and development",
    "capex": "capital expenditures",
    "capital spending": "capital expenditures",
    "sg&a": "selling general and administrative",
    "eps": "earnings per share",
    "top line": "revenue",
    "bottom line": "net income",
    "buyback": "share repurchase",
}

_LAST_N = re.compile(
    r"\b(?:last|past|previous|trailing)\s+(\w+)\s+(year|years|quarter|quarters|fiscal years?)\b",
    re.IGNORECASE,
)
_SINCE = re.compile(r"\bsince\s+(?:fy)?(\d{4})\b", re.IGNORECASE)
_EXPLICIT_YEAR = re.compile(r"\b(?:fy)?(20\d{2})\b", re.IGNORECASE)
_LATEST = re.compile(r"\b(latest|current|most recent|this (year|quarter)|today)\b", re.IGNORECASE)
_OVER_TIME = re.compile(
    r"\b(over time|over the (last|past)|recent (fiscal )?years|trend|grow(n|th)?|"
    r"chang(e|ed|ing)|history|"
    r"year[- ]over[- ]year|yoy|each year|annually)\b",
    re.IGNORECASE,
)
_WORD_NUMS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _n(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _WORD_NUMS.get(token.lower())


def _expand_synonyms(text: str) -> str:
    lowered = text.lower()
    additions = [alias for term, alias in _SYNONYMS.items() if term in lowered]
    return f"{text} {' '.join(additions)}".strip() if additions else text


def _resolve_years(question: str, available: list[int]) -> list[int]:
    """Map a relative/explicit time reference onto the company's actual fiscal years."""
    if not available:
        return []
    latest = max(available)

    if _LATEST.search(question):
        return [latest]

    m = _LAST_N.search(question)
    if m:
        count = _n(m.group(1))
        unit = m.group(2).lower()
        if count:
            span = count if unit.startswith(("year", "fiscal")) else max(1, (count + 3) // 4)
            return [y for y in available if y > latest - span]

    since = _SINCE.search(question)
    if since:
        start = int(since.group(1))
        return [y for y in available if y >= start]

    explicit = sorted({int(y) for y in _EXPLICIT_YEAR.findall(question)} & set(available))
    if explicit:
        return explicit
    return []


def _decompose(question: str, intent: Intent, expanded: str) -> list[str]:
    """Split multi-part questions and add intent-specific sub-queries (§16.3 example)."""
    parts = [p.strip() for p in re.split(r"\?|\band\b|;", question) if len(p.strip()) > 8]
    subs: list[str] = parts[1:] if len(parts) > 1 else []
    if intent == Intent.FINANCIAL_EXPLANATION:
        # "why did revenue grow" → history + MD&A drivers + management commentary (§16.3).
        subs += [
            f"{expanded} management discussion and analysis drivers",
            f"{expanded} segment breakdown",
            f"{expanded} management commentary outlook",
        ]
    return list(dict.fromkeys(subs))  # dedup while preserving order


def rewrite_query(
    question: str, *, intent: Intent, available_fiscal_years: list[int]
) -> RewrittenQuery:
    """Rewrite a question for retrieval. Pure: time resolution uses the passed fiscal years."""
    expanded = _expand_synonyms(question)
    years = _resolve_years(question, available_fiscal_years)
    is_over_time = bool(_OVER_TIME.search(question)) or len(years) >= 2
    single_latest = bool(available_fiscal_years) and years == [max(available_fiscal_years)]
    is_latest = (bool(_LATEST.search(question)) or single_latest) and not is_over_time
    return RewrittenQuery(
        original=question,
        expanded=expanded,
        sub_queries=_decompose(question, intent, expanded),
        fiscal_years=years,
        is_over_time=is_over_time,
        is_latest=is_latest,
    )
