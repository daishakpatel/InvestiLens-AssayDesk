"""Shared RAG pipeline types (Phase 2c, §16).

Small, typed, framework-free data structures passed between the pipeline's stages (ADR-0003). All
scores are retrieval signals (floats are fine, DR: never for money); monetary values stay in the
deterministic finance layer and arrive as `MetricResult`/`SourceRecord`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from app.schemas.sources import SourceRecord


class Intent(StrEnum):
    """Query intents (RAG-002). Intent selects the retrieval strategy and token budget."""

    FINANCIAL_METRIC = "FINANCIAL_METRIC"
    RISK_ANALYSIS = "RISK_ANALYSIS"
    FINANCIAL_EXPLANATION = "FINANCIAL_EXPLANATION"
    DOCUMENT_SUMMARY = "DOCUMENT_SUMMARY"
    MANAGEMENT_COMMENTARY = "MANAGEMENT_COMMENTARY"
    COMPARISON = "COMPARISON"
    FILING_DIFF = "FILING_DIFF"
    OUT_OF_SCOPE_ADVICE = "OUT_OF_SCOPE_ADVICE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


@dataclass(frozen=True)
class SearchScope:
    """Limits a search to some sections (`chunk_metadata.section_key`) and to filings on or after
    a date. Report sections use it so old filings, proxies and press-release boilerplate stay out
    (ADR-0053)."""

    section_keys: frozenset[str] = frozenset()  # empty = any section
    filed_since: date | None = None


@dataclass(frozen=True)
class IntentResult:
    intent: Intent
    confidence: float
    rule: str  # which rule/classifier fired — logged for evaluation (RAG-002)


@dataclass
class RewrittenQuery:
    """Output of query rewriting (RAG-003)."""

    original: str
    expanded: str  # synonym/abbreviation-expanded text used for retrieval
    sub_queries: list[str] = field(default_factory=list)
    fiscal_years: list[int] = field(default_factory=list)  # resolved time range, empty if none
    is_over_time: bool = False  # "how has X changed" → period-stratified retrieval (RAG-014)
    is_latest: bool = False  # "latest"/"current" → recency boost (RAG-014)


@dataclass
class Evidence:
    """One retrieved chunk with its anchor, scores, and (optional) expanded parent context."""

    chunk_id: int
    document_id: int
    source: SourceRecord  # backend-issued source_id + typed anchor (ADR-0004)
    text: str
    section: str | None
    tier: int
    filing_type: str | None
    filing_date: date | None
    period_end: date | None
    parent_section_id: str | None
    dedup_hash: str | None = None
    scores: dict[str, float] = field(default_factory=dict)  # vector/keyword/rrf/rerank
    expanded_context: str | None = None  # parent/neighbor text (RAG-015); citation stays the child
    unchanged_since: int | None = None  # boilerplate "unchanged since <year>" (RAG-016)

    @property
    def source_id(self) -> str:
        return self.source.source_id

    @property
    def rerank_score(self) -> float:
        return self.scores.get("rerank", self.scores.get("rrf", 0.0))


@dataclass
class RetrievalResult:
    """The pipeline's contract with Phase 3: ranked evidence + source IDs + scores (§16.5)."""

    question: str
    intent: Intent
    rewritten: RewrittenQuery
    evidence: list[Evidence]
    structured_source_ids: list[str] = field(default_factory=list)
    assembled_context: str = ""
    sufficient: bool = True
    abstain_reason: str | None = None
    # Which tools ran, for the chat tool-trace panel (RAG-031 logging): {"tool", "latency_ms"}.
    tool_trace: list[dict[str, object]] = field(default_factory=list)
