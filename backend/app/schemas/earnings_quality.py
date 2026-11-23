"""Earnings-quality dashboard response (Phase 7d, FR-EQ-009/010)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel

from app.schemas.common import Freshness
from app.schemas.sources import MetricInput

QualityStatus = Literal["ok", "insufficient_data", "not_applicable"]


class QualityPoint(BaseModel):
    """One measure for one period. `value` is None unless `status` is ok (never a zero)."""

    period: str  # "FY2026", or "TTM-Q2-FY2026" for a trailing figure
    fiscal_year: int
    value: Decimal | None
    status: QualityStatus
    # Beneish: below_threshold | above_threshold | insufficient_data; Altman: distress | grey | safe
    band: str | None = None
    reason: str | None = None  # reason code when not ok (MISSING_INPUT, NEGATIVE_BASE, ...)
    reason_text: str | None = None  # a sentence for the UI, e.g. "Not computed for banks: ..."
    missing: list[str] = []  # the inputs that were not reported
    source_id: str | None = None
    formula_id: str | None = None
    inputs: list[MetricInput] = []  # every figure used, each with its backend-issued source ID
    details: dict[str, Any] = {}  # Beneish indices, Altman model_variant, Piotroski criteria


class QualityMeasure(BaseModel):
    metric_name: str
    label: str
    unit: str
    explanation: str
    caveat: str | None = None  # the screening-heuristic caveat, on scores only (FR-EQ-013)
    points: list[QualityPoint]  # fiscal years, oldest first
    latest_ttm: QualityPoint | None = None  # trailing twelve months, where stored
    # Fraction of the peer set (target included) at or below the target's latest value; None when
    # fewer than three peers report it (FR-EQ-010).
    peer_percentile: Decimal | None = None
    peer_count: int = 0
    # The calendar year compared (NVIDIA's January FY2026 is 2025), ADR-0070.
    peer_calendar_year: int | None = None


class EarningsQualityResponse(BaseModel):
    ticker: str
    fiscal_years: list[int]
    measures: list[QualityMeasure]
    peers: list[str]
    notes: list[str] = []  # degradations, surfaced (e.g. peer percentile hidden)
    freshness: Freshness
