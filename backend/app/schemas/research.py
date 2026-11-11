"""Research report output schema (spec §17.2), copied field-for-field.

Backend-only fields (`confidence_label`, `internal_confidence`, `change_status`) are set by the
backend after verification (CIT-005); the LLM MUST NOT populate them. `internal_confidence` is
never rendered raw to the client.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.company import MetricSeriesPoint

EvidenceLabel = Literal["strongly_supported", "supported", "limited_evidence"]


class ResearchClaim(BaseModel):
    claim_id: str
    text: str
    source_ids: list[str] = Field(min_length=1)  # CIT-004: every claim cites ≥1 source
    confidence_label: EvidenceLabel | None = None  # set by backend, not LLM
    internal_confidence: float | None = None  # backend only, never shown raw


class Risk(BaseModel):
    category: Literal[
        "business",
        "competitive",
        "regulatory",
        "supply_chain",
        "customer_concentration",
        "geographic",
        "technology",
        "litigation",
        "capital_allocation",
        "macroeconomic",
    ]
    description: str
    source_ids: list[str] = Field(min_length=1)
    change_status: Literal["new", "unchanged", "reworded", "removed"] | None = None
    evidence_label: EvidenceLabel | None = None


class ManagementTopicStatement(BaseModel):
    topic: Literal[
        "ai_demand",
        "revenue_outlook",
        "margins",
        "capital_spending",
        "competition",
        "regulation",
        "product_roadmap",
    ]
    period: str  # e.g. "Q3 FY2026"
    text: str
    speaker: str | None = None
    source_ids: list[str] = Field(min_length=1)


class Factor(BaseModel):
    """A bull or bear factor."""

    title: str
    claims: list[ResearchClaim]
    counterpoint_source_ids: list[str] = []
    monitoring_indicator: str | None = None  # derived from backend metrics


class NewsItemSummary(BaseModel):
    news_id: str
    summary: str  # 2 sentences max
    category: str
    source_ids: list[str] = Field(min_length=1)


class CarriedOverSections(BaseModel):
    """Sections copied unchanged from an earlier report because their model call failed now."""

    research_id: str
    generated_at: datetime
    sections: list[str]


class ResearchReport(BaseModel):
    executive_summary: list[ResearchClaim]
    company_overview: list[ResearchClaim]
    revenue_analysis: list[ResearchClaim]
    profitability_analysis: list[ResearchClaim]
    balance_sheet_analysis: list[ResearchClaim]
    cash_flow_analysis: list[ResearchClaim]
    valuation_analysis: list[ResearchClaim]
    news_summary: list[NewsItemSummary]
    risks: list[Risk]
    management_commentary: list[ManagementTopicStatement]
    bull_factors: list[Factor]
    bear_factors: list[Factor]
    insufficient_evidence_sections: list[str] = []
    # ADR-0057: sections whose model call failed in this run. A failed section is filled from the
    # previous report when that report wrote it (`carried_over`), else listed in `failed_sections`.
    failed_sections: list[str] = []
    carried_over: CarriedOverSections | None = None


class ReportDiffItem(BaseModel):
    """A claim sharing no cited source with any claim of its section in the other report."""

    text: str
    source_ids: list[str] = Field(min_length=1)


class ReportSectionChange(BaseModel):
    section: str  # a ResearchReport field name, e.g. "revenue_analysis"
    added: list[ReportDiffItem]  # in this report only
    removed: list[ReportDiffItem]  # in the earlier report only


class ReportMetricChange(BaseModel):
    """The latest period of a metric each report cites, where the two differ (ADR-0059)."""

    metric_name: str
    period_type: str  # FY|Q|TTM|LATEST
    before: MetricSeriesPoint | None  # None: the earlier report did not cite this metric
    after: MetricSeriesPoint | None  # None: this report no longer cites it


class ReportDiff(BaseModel):
    """GET /research/{id}/diff: what changed since an earlier report (spec §10.16)."""

    research_id: str
    against_id: str
    against_generated_at: datetime | None
    data_version_changed: bool
    prompt_version_changed: bool
    sections: list[ReportSectionChange]  # only sections with a change
    metrics: list[ReportMetricChange]
    # Failed in either report (ADR-0056): an empty section is not a change, so not compared.
    skipped_sections: list[str] = []
