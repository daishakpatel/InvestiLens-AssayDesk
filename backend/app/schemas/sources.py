"""Source records and derived-metric results (spec §13.3, §12 DR-040).

Source IDs are backend-issued (CIT-001). Each `source_type` carries its own anchor fields
(CIT-002). SEC filings are HTML with no stable pagination, so `page` is nullable and set only
for PDF sources (ADR-0004). The variants form a discriminated union on `source_type`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field

SourceType = Literal[
    "text_chunk",
    "table_chunk",
    "xbrl_fact",
    "derived_metric",
    "news_item",
    "price_bar",
    "earnings_release",
    "transcript",
]


class _SourceBase(BaseModel):
    source_id: str
    tier: int = Field(ge=1, le=5, description="Source-quality tier (§15)")
    url: str | None = None


class TextChunkSource(_SourceBase):
    source_type: Literal["text_chunk"] = "text_chunk"
    document_id: str
    section_path: list[str] = []
    paragraph_id: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    page: int | None = None  # PDFs only
    text: str | None = None


class TableChunkSource(_SourceBase):
    source_type: Literal["table_chunk"] = "table_chunk"
    document_id: str
    table_id: str
    row_labels: list[str] = []
    col_labels: list[str] = []
    cell_refs: list[str] = []


class XbrlFactSource(_SourceBase):
    source_type: Literal["xbrl_fact"] = "xbrl_fact"
    accession_number: str
    concept_tag: str
    context_id: str | None = None
    unit: str | None = None
    value: Decimal | None = None


class DerivedMetricSource(_SourceBase):
    source_type: Literal["derived_metric"] = "derived_metric"
    formula_id: str
    formula_version: str
    input_source_ids: list[str] = []
    value: Decimal | None = None
    period: str | None = None


class NewsItemSource(_SourceBase):
    source_type: Literal["news_item"] = "news_item"
    news_id: str
    publisher: str | None = None
    published_at: str | None = None
    excerpt_span: str | None = None


class PriceBarSource(_SourceBase):
    """End-of-day market data: one trading day (`price:{TICKER}:{date}`) or two
    (`price:{TICKER}:{date}:{end_date}`), ADR-0054."""

    source_type: Literal["price_bar"] = "price_bar"
    ticker: str
    date: str  # ISO-8601 trading day
    end_date: str | None = None
    provider: str | None = None


class EarningsReleaseSource(_SourceBase):
    """Earnings release or transcript (transcripts add `speaker`)."""

    source_type: Literal["earnings_release", "transcript"]
    document_id: str
    paragraph_id: str | None = None
    speaker: str | None = None
    char_start: int | None = None
    char_end: int | None = None


SourceRecord = Annotated[
    TextChunkSource
    | TableChunkSource
    | XbrlFactSource
    | DerivedMetricSource
    | NewsItemSource
    | PriceBarSource
    | EarningsReleaseSource,
    Field(discriminator="source_type"),
]


class MetricInput(BaseModel):
    """One input to a derived metric, linking to the source it came from (DR-040)."""

    name: str
    value: Decimal | None = None
    source_id: str | None = None


class MetricResult(BaseModel):
    """Deterministic metric output with lineage (DR-040).

    A `None` value means the metric could not be computed; `warnings` carries the reason code
    (`NEGATIVE_BASE`, `MISSING_INPUT`, `NOT_APPLICABLE_SECTOR`) per DR-041.
    """

    value: Decimal | None
    unit: str
    inputs: list[MetricInput] = []
    formula_id: str
    formula_version: str | None = None
    warnings: list[str] = []
