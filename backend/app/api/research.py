"""Research job endpoints: async generation, polling, SSE progress, report retrieval.

Follows the async job pattern (spec §25.1): POST enqueues and returns 202 + ids; progress via
polling or SSE. `Idempotency-Key` single-flights duplicate requests (API-003, JOB-005). Actual
generation runs on the Phase 5d worker (ADR-0017); until then an enqueued job stays `queued`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user_id
from app.billing.budget import check_budget
from app.config import get_settings
from app.db import get_db
from app.models import Company, Job
from app.models import ResearchReport as ResearchReportRow
from app.repositories import companies as company_repo
from app.repositories import jobs as job_repo
from app.repositories import reports as report_repo
from app.repositories import users as user_repo
from app.research.diff import diff_reports
from app.research.prompts import REPORT_PROMPT_VERSION
from app.schemas.jobs import (
    JobState,
    JobStatus,
    ResearchAccepted,
    ResearchReportEnvelope,
    ResearchRequest,
)
from app.schemas.research import ReportDiff, ResearchReport
from app.utils.logging import get_logger, log_event

router = APIRouter(prefix="/research", tags=["Research"])
logger = get_logger(__name__)

_DB = Depends(get_db, scope="function")
_USER = Depends(get_current_user_id)
_JOB_TYPE = "research_report"

# jobs.status (queued|running|done|failed) → the API's JobStatus vocabulary.
_STATUS_MAP: dict[str, JobStatus] = {
    "queued": "queued",
    "running": "processing",
    "done": "done",
    "failed": "failed",
}


def _empty_report() -> ResearchReport:
    """A valid, all-empty report for a job that has not generated yet (honest `queued` state)."""
    return ResearchReport(
        executive_summary=[],
        company_overview=[],
        revenue_analysis=[],
        profitability_analysis=[],
        balance_sheet_analysis=[],
        cash_flow_analysis=[],
        valuation_analysis=[],
        news_summary=[],
        risks=[],
        management_commentary=[],
        bull_factors=[],
        bear_factors=[],
        insufficient_evidence_sections=[],
    )


def _accepted(job: Job) -> ResearchAccepted:
    return ResearchAccepted(
        research_id=str(job.result_ref) if job.result_ref else "",
        job_id=str(job.id),
        status=_STATUS_MAP.get(job.status, "processing"),
    )


@router.post("", status_code=202, response_model=ResearchAccepted)
def create_research(
    body: ResearchRequest,
    db: Session = _DB,
    user_id: int = _USER,  # anonymous users may not generate reports (ADR-0006)
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ResearchAccepted:
    company = company_repo.get_by_ticker(db, body.ticker)
    if company is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"company {body.ticker.upper()} not ingested"
        )

    # Fast-fail before enqueueing a job the budget won't allow to finish (NFR-008, ADR-0022).
    user = user_repo.get_active(db, user_id)
    if user is not None:
        check_budget(db, user)

    # Single-flight: an idempotency-key match, else any in-flight job for the same company.
    if idempotency_key:
        existing = job_repo.find_by_idempotency_key(db, job_type=_JOB_TYPE, key=idempotency_key)
        if existing is not None:
            return _accepted(existing)
    active = job_repo.find_active_for_company(db, job_type=_JOB_TYPE, ticker=company.ticker)
    if active is not None:
        return _accepted(active)

    settings = get_settings()
    report = report_repo.create_report(
        db,
        company_id=company.id,
        user_id=user_id,  # saved-report association (§26.2, scope item 11)
        model=settings.llm_model_strong,
        prompt_version=REPORT_PROMPT_VERSION,
        data_version="pending",
        status="pending",
    )
    job = job_repo.create_job(
        db,
        job_type=_JOB_TYPE,
        params={
            "ticker": company.ticker,
            "idempotency_key": idempotency_key,
            "force_refresh": body.force_refresh,
        },
        created_by=user_id,
        result_ref=str(report.id),
    )
    db.commit()
    # Dispatch to the interactive worker when background jobs are enabled (ADR-0024); otherwise
    # the row stays `queued` exactly as in Phase 4a (no broker in tests/CI).
    if settings.background_jobs_enabled:
        from app.tasks.interactive import generate_research_report

        generate_research_report.delay(company.ticker, job.id, user_id)
    return _accepted(job)


@router.get("/jobs/{job_id}", response_model=JobState)
def get_job(job_id: int, db: Session = _DB) -> JobState:
    job = job_repo.get_job(db, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return JobState(
        status=_STATUS_MAP.get(job.status, "processing"),
        progress=job.progress or 0,
        stage=job.status,
        stages=[],
    )


@router.get("/jobs/{job_id}/events")
def job_events(job_id: int, db: Session = _DB) -> StreamingResponse:
    """SSE stream of job progress (API-004). Emits the current state, then closes."""
    job = job_repo.get_job(db, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    status_str = _STATUS_MAP.get(job.status, "processing")
    progress = job.progress or 0

    async def _stream() -> AsyncIterator[bytes]:
        frame = f'event: progress\ndata: {{"progress": {progress}, "stage": "{status_str}"}}\n\n'
        yield frame.encode()

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.get("/{research_id}", response_model=ResearchReportEnvelope)
def get_research(research_id: int, db: Session = _DB) -> ResearchReportEnvelope:
    report = report_repo.get_report(db, research_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    company = db.get(Company, report.company_id)
    return to_envelope(report, company.ticker if company else "")


def to_envelope(report: ResearchReportRow, ticker: str) -> ResearchReportEnvelope:
    """The API envelope for a stored report."""
    return ResearchReportEnvelope(
        research_id=str(report.id),
        ticker=ticker,
        generated_at=report.generated_at.isoformat() if report.generated_at else None,
        model=report.model,
        prompt_version=report.prompt_version,
        data_version=report.data_version,
        report=report_body(report),
    )


def report_body(report: ResearchReportRow) -> ResearchReport:
    """A stored report's body; one that no longer validates is a logged 409."""
    payload = report.report_json
    try:
        return ResearchReport.model_validate(payload) if payload else _empty_report()
    except ValidationError as exc:
        # An older schema's report is unreadable, not a server error; the UI offers a new one.
        log_event(
            logger,
            logging.WARNING,
            "research.report_invalid",
            research_id=report.id,
            prompt_version=report.prompt_version,
            errors=exc.error_count(),
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the stored report no longer matches the current format; generate a new one",
        ) from exc


@router.get("/{research_id}/diff", response_model=ReportDiff)
def diff_research(research_id: int, against: int | None = None, db: Session = _DB) -> ReportDiff:
    """What changed since an earlier report of the same company; by default the previous one."""
    report = report_repo.get_report(db, research_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    other = (
        report_repo.get_previous_complete(db, report)
        if against is None
        else report_repo.get_report(db, against)
    )
    if other is None or other.company_id != report.company_id or other.id == report.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no earlier report of this company")
    return diff_reports(
        db, row=report, report=report_body(report), against_row=other, against=report_body(other)
    )
