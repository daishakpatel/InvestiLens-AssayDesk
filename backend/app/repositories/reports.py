"""research_reports + research_sources persistence (Phase 3b, §17.2).

A report row records `model`, `prompt_version`, and `data_version` for reproducibility (NFR-010);
`research_sources` stores every cited source so the report renders without re-resolving anchors.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ResearchReport, ResearchSource
from app.schemas.citations import Citation


def get_report(session: Session, report_id: int) -> ResearchReport | None:
    return session.get(ResearchReport, report_id)


def get_latest_complete(session: Session, *, company_id: int) -> ResearchReport | None:
    """The newest completed report for a company (for `/research/latest`, §24.2)."""
    return session.scalar(
        select(ResearchReport)
        .where(
            ResearchReport.company_id == company_id,
            ResearchReport.status == "complete",
        )
        .order_by(ResearchReport.generated_at.desc(), ResearchReport.id.desc())
    )


def get_previous_complete(session: Session, report: ResearchReport) -> ResearchReport | None:
    """The company's newest completed report created before `report` (ADR-0059)."""
    return session.scalar(
        select(ResearchReport)
        .where(
            ResearchReport.company_id == report.company_id,
            ResearchReport.status == "complete",
            ResearchReport.id < report.id,
        )
        .order_by(ResearchReport.id.desc())
    )


def create_report(
    session: Session,
    *,
    company_id: int,
    model: str,
    prompt_version: str,
    data_version: str,
    status: str = "running",
    user_id: int | None = None,
    supersedes_report_id: int | None = None,
) -> ResearchReport:
    report = ResearchReport(
        company_id=company_id,
        user_id=user_id,
        model=model,
        prompt_version=prompt_version,
        data_version=data_version,
        status=status,
        generated_at=datetime.now(UTC),
        supersedes_report_id=supersedes_report_id,
    )
    session.add(report)
    session.flush()
    return report


def finish_report(
    session: Session,
    report: ResearchReport,
    *,
    report_json: dict[str, Any],
    status: str,
    cost_usd: Decimal | None = None,
    latency_ms: int | None = None,
) -> None:
    report.report_json = report_json
    report.status = status
    report.cost_usd = cost_usd
    report.latency_ms = latency_ms


def copy_sources(
    session: Session, *, from_report_id: int, to_report_id: int, source_ids: set[str]
) -> int:
    """Copy an earlier report's stored sources among `source_ids` (carried over, ADR-0057)."""
    rows = session.scalars(
        select(ResearchSource).where(
            ResearchSource.report_id == from_report_id, ResearchSource.source_id.in_(source_ids)
        )
    ).all()
    session.add_all(
        ResearchSource(
            report_id=to_report_id,
            source_id=r.source_id,
            source_type=r.source_type,
            citation_text=r.citation_text,
            url=r.url,
            page=r.page,
            section_path=r.section_path,
            char_start=r.char_start,
            char_end=r.char_end,
            tier=r.tier,
        )
        for r in rows
    )
    return len(rows)


def record_sources(session: Session, report_id: int, citations: list[Citation]) -> int:
    """Persist the report's cited sources (CIT-001). Returns the count."""
    rows = [
        ResearchSource(
            report_id=report_id,
            source_id=c.source_id,
            source_type=c.source_type,
            citation_text=c.citation_text,
            url=c.url,
            tier=c.tier,
        )
        for c in citations
    ]
    session.add_all(rows)
    return len(rows)
