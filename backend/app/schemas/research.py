"""Research report output schema (spec §17.2), copied field-for-field.

Backend-only fields (`confidence_label`, `internal_confidence`, `change_status`) are set by the
backend after verification (CIT-005); the LLM MUST NOT populate them. `internal_confidence` is
never rendered raw to the client.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

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
