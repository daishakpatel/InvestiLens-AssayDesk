"""Marker parsing, claim segmentation, and numeric/date extraction (CIT-004, CIT-005 L2).

The LLM emits `[SOURCE:<id>]` markers (CIT-001); here we strip them into per-claim source-id lists,
split prose into sentence-level claims, and pull out the numbers/percentages/dates a claim asserts
so the deterministic numeric-match layer can check each against its cited source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# Models also write `[source_id=a, source_id=b]`, `[cite: a; b]` and a bare `[news:12]`.
_LABEL = r"(?:source_id|source|cite)\s*[:=]\s*"
_BARE_ID = r"(?=(?:news|xbrl|derived|price|redflag):\S)"
MARKER = re.compile(rf"\[(?:{_LABEL}|{_BARE_ID})([^\]]+?)\s*\]", re.IGNORECASE)
_ID_LABEL = re.compile(rf"^{_LABEL}", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9$])")

_SCALES = {
    "trillion": Decimal(10) ** 12,
    "tn": Decimal(10) ** 12,
    "billion": Decimal(10) ** 9,
    "bn": Decimal(10) ** 9,
    "b": Decimal(10) ** 9,  # "5.4B" (Phase 7i guidance)
    "million": Decimal(10) ** 6,
    "mm": Decimal(10) ** 6,
    "mn": Decimal(10) ** 6,
    "thousand": Decimal(10) ** 3,
    "k": Decimal(10) ** 3,
}

_PERCENT = re.compile(r"(\d[\d,]*\.?\d*)\s*(%|percent|percentage points?|bps)", re.IGNORECASE)
_SCALED = re.compile(
    r"\$?\s*(\d[\d,]*\.?\d*)\s*(trillion|billion|million|thousand|tn|bn|mm|mn|k|b)\b",
    re.IGNORECASE,
)
_CURRENCY = re.compile(r"\$\s*(\d[\d,]*\.?\d*)")
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_PLAIN = re.compile(r"(?<![\w.])(\d[\d,]*\.?\d*)(?![\w%])")

# A sentence is "factual" (so it needs a citation, CIT-005 L5) if it asserts a number/date or a
# finance term; purely connective sentences ("This section covers…") do not.
_FACT_TERMS = re.compile(
    r"\b(revenue|margin|income|earnings|eps|cash|debt|growth|grew|increased|decreased|declined|"
    r"rose|fell|risk|guidance|ebitda|dividend|billion|million|percent)s?\b",  # "risks", "margins"
    re.IGNORECASE,
)
_CAUSAL = re.compile(
    r"\b(because|due to|driven by|led to|resulted in|as a result|caused by|caused|attributable to|"
    r"thanks to|owing to|drove)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NumberToken:
    raw: str
    value: Decimal | None  # None for a bare date
    kind: str  # "percent" | "money" | "number" | "date"


@dataclass
class RawClaim:
    claim_id: str
    text: str  # marker-free, trimmed
    source_ids: list[str] = field(default_factory=list)


def _to_decimal(digits: str) -> Decimal | None:
    try:
        return Decimal(digits.replace(",", ""))
    except InvalidOperation:
        return None


def strip_markers(text: str) -> tuple[str, list[str]]:
    """Return (text without markers, source_ids in first-appearance order, de-duplicated)."""
    ids: list[str] = []
    for m in MARKER.finditer(text):
        for part in re.split(r"[,;]", m.group(1)):
            sid = _ID_LABEL.sub("", part.strip())
            if sid and sid not in ids:
                ids.append(sid)
    cleaned = MARKER.sub("", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = re.sub(r"\s+([.,;%])", r"\1", cleaned)  # tidy space left before punctuation
    return cleaned, ids


def split_claims(text: str) -> list[RawClaim]:
    """Split prose into sentence-level claims, each with its cited source IDs (CIT-004)."""
    claims: list[RawClaim] = []
    for i, sentence in enumerate(_SENTENCE_SPLIT.split(text.strip())):
        if not sentence.strip():
            continue
        cleaned, ids = strip_markers(sentence)
        if cleaned:
            claims.append(RawClaim(claim_id=f"c{i}", text=cleaned, source_ids=ids))
    return claims


def extract_numbers(text: str) -> list[NumberToken]:
    """Extract numbers/percentages/money/dates from claim text, normalizing scale words."""
    tokens: list[NumberToken] = []
    consumed: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(s < end and start < e for s, e in consumed)

    def take(match: re.Match[str], value: Decimal | None, kind: str) -> None:
        if not overlaps(match.start(), match.end()):
            consumed.append((match.start(), match.end()))
            tokens.append(NumberToken(raw=match.group(0).strip(), value=value, kind=kind))

    for m in _DATE.finditer(text):
        take(m, None, "date")
    for m in _PERCENT.finditer(text):
        take(m, _to_decimal(m.group(1)), "percent")
    for m in _SCALED.finditer(text):
        base = _to_decimal(m.group(1))
        scale = _SCALES[m.group(2).lower()]
        take(m, base * scale if base is not None else None, "money")
    for m in _CURRENCY.finditer(text):
        take(m, _to_decimal(m.group(1)), "money")
    for m in _PLAIN.finditer(text):
        take(m, _to_decimal(m.group(1)), "number")
    return tokens


def has_factual_content(text: str) -> bool:
    """True if a sentence asserts a fact (number/date/finance term) and so requires a citation."""
    return bool(extract_numbers(text) or _FACT_TERMS.search(text))


def is_causal(text: str) -> bool:
    """True if the claim asserts causation (triggers the scope check, CIT-005 L4)."""
    return bool(_CAUSAL.search(text))


def soften_causal(text: str) -> str:
    """Convert causal language to correlation language (CIT-005 L4 soften path)."""
    return re.sub(r"\s{2,}", " ", _CAUSAL.sub("amid", text)).strip()
