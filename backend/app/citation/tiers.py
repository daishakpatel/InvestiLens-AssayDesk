"""Source-quality hierarchy (§15).

Five tiers, Tier 1 = highest. Tier is stored on every document/source; this module is the single
lookup used by both retrieval reranking (Phase 2c) and confidence scoring (HAL-002). A lower tier
number is better; `tier_score` maps it to [0, 1] for the confidence formula.
"""

from __future__ import annotations

# source_type / document_type → tier (§15).
_TYPE_TIER: dict[str, int] = {
    # Tier 1 — SEC filings and their XBRL/derived facts.
    "text_chunk": 1,
    "table_chunk": 1,
    "xbrl_fact": 1,
    "derived_metric": 1,
    "filing": 1,
    # Tier 2 — company earnings releases.
    "earnings_release": 2,
    # Tier 3 — IR materials / transcripts.
    "transcript": 3,
    "ir_material": 3,
    # Tier 4 — licensed/reputable news.
    "news_item": 4,
    "news": 4,
    "price_bar": 4,  # licensed end-of-day market data (ADR-0054)
}

MIN_TIER = 1
MAX_TIER = 5


def tier_for_source_type(source_type: str) -> int:
    """Tier for a source/document type; unknown types fall to Tier 5 (other)."""
    return _TYPE_TIER.get(source_type, MAX_TIER)


def tier_score(tier: int) -> float:
    """Map a tier (1..5) to [0, 1] for confidence scoring: Tier 1 = 1.0, Tier 5 = 0.2."""
    clamped = max(MIN_TIER, min(MAX_TIER, tier))
    return (MAX_TIER + 1 - clamped) / MAX_TIER
