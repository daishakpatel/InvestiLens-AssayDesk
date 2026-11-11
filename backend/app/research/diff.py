"""What changed between two reports of one company (spec §10.16, ADR-0059).

Deterministic, no model call. Model wording differs on every run, so claims are matched on
evidence: a claim is unchanged when it shares a cited source with a claim in the same section of
the other report. Metric changes are the latest period of each cited metric, valued from the
stored rows (no LLM numbers).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FinancialMetric
from app.models import ResearchReport as ResearchReportRow
from app.schemas.company import MetricSeriesPoint
from app.schemas.research import (
    NewsItemSummary,
    ReportDiff,
    ReportDiffItem,
    ReportMetricChange,
    ReportSectionChange,
    ResearchReport,
    Risk,
)

SECTIONS = (
    "executive_summary",
    "company_overview",
    "revenue_analysis",
    "profitability_analysis",
    "balance_sheet_analysis",
    "cash_flow_analysis",
    "valuation_analysis",
    "news_summary",
    "risks",
    "management_commentary",
    "bull_factors",
    "bear_factors",
)
_FACTORS = ("bull_factors", "bear_factors")


def _items(report: ResearchReport, section: str) -> list[ReportDiffItem]:
    entries = getattr(report, section)
    if section in _FACTORS:
        return [
            ReportDiffItem(text=f"{factor.title}: {claim.text}", source_ids=claim.source_ids)
            for factor in entries
            for claim in factor.claims
        ]
    return [ReportDiffItem(text=_text(entry), source_ids=entry.source_ids) for entry in entries]


def _text(entry: object) -> str:
    if isinstance(entry, NewsItemSummary):
        return entry.summary
    if isinstance(entry, Risk):
        return entry.description
    return str(getattr(entry, "text", ""))


def _only_in(items: list[ReportDiffItem], other: list[ReportDiffItem]) -> list[ReportDiffItem]:
    # shortcut: one shared source = same claim; a claim that also gained new sources is hidden,
    # and its newer figures show in the metric changes instead (ADR-0059).
    cited = {sid for item in other for sid in item.source_ids}
    return [item for item in items if cited.isdisjoint(item.source_ids)]


def _source_ids(report: ResearchReport) -> set[str]:
    return {sid for s in SECTIONS for item in _items(report, s) for sid in item.source_ids}


def _latest_cited(
    session: Session, company_id: int, ids: set[str]
) -> dict[tuple[str, str], FinancialMetric]:
    """Per (metric, period type), the cited stored row with the latest period end."""
    latest: dict[tuple[str, str], FinancialMetric] = {}
    if not ids:
        return latest
    rows = session.scalars(
        select(FinancialMetric).where(
            FinancialMetric.company_id == company_id,
            FinancialMetric.source_id.in_(ids),
            FinancialMetric.is_latest.is_(True),
        )
    )
    for row in rows:
        key = (row.metric_name, row.period_type)
        held = latest.get(key)
        if held is None or (row.period_end or date.min) > (held.period_end or date.min):
            latest[key] = row
    return latest


def _point(row: FinancialMetric | None) -> MetricSeriesPoint | None:
    if row is None:
        return None
    return MetricSeriesPoint(
        period=row.period,
        period_end=row.period_end.isoformat() if row.period_end else None,
        value=row.metric_value,
        unit=row.unit,
        source_id=row.source_id,
    )


def diff_reports(
    session: Session,
    *,
    row: ResearchReportRow,
    report: ResearchReport,
    against_row: ResearchReportRow,
    against: ResearchReport,
) -> ReportDiff:
    failed = set(report.failed_sections) | set(against.failed_sections)
    skipped = [s for s in SECTIONS if s in failed]
    sections = []
    for section in SECTIONS:
        if section in failed:
            continue
        new, old = _items(report, section), _items(against, section)
        added, removed = _only_in(new, old), _only_in(old, new)
        if added or removed:
            sections.append(ReportSectionChange(section=section, added=added, removed=removed))

    after = _latest_cited(session, row.company_id, _source_ids(report))
    before = _latest_cited(session, row.company_id, _source_ids(against))
    metrics = []
    for key in sorted(before.keys() | after.keys()):
        b, a = before.get(key), after.get(key)
        if b is None or a is None or a.period != b.period:
            metrics.append(
                ReportMetricChange(
                    metric_name=key[0], period_type=key[1], before=_point(b), after=_point(a)
                )
            )
    return ReportDiff(
        research_id=str(row.id),
        against_id=str(against_row.id),
        against_generated_at=against_row.generated_at,
        data_version_changed=row.data_version != against_row.data_version,
        prompt_version_changed=row.prompt_version != against_row.prompt_version,
        sections=sections,
        metrics=metrics,
        skipped_sections=skipped,
    )
