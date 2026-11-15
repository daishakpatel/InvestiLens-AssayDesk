"""Admin (internal) endpoints (spec §24.2). Role-gated via `require_admin` (ADR-0006 seam)."""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.api.pagination import NEXT_CURSOR_HEADER, clamp_limit, decode_cursor, encode_cursor
from app.config import get_settings
from app.db import get_db
from app.repositories import claim_verifications as claim_repo
from app.repositories import companies as company_repo
from app.repositories import eval_runs as eval_repo
from app.repositories import ingestion as ingestion_repo
from app.repositories import jobs as job_repo
from app.repositories import reports as report_repo
from app.schemas.admin import (
    EvalRunSummary,
    IngestionRunSummary,
    RefreshAccepted,
    RejectedClaim,
    RejectedClaims,
)

router = APIRouter(prefix="/admin", tags=["Admin"])

_DB = Depends(get_db, scope="function")
_ADMIN = Depends(require_admin)


@router.get("/ingestion-runs", response_model=list[IngestionRunSummary])
def list_ingestion_runs(
    response: Response,
    limit: int | None = None,
    cursor: str | None = None,
    db: Session = _DB,
    _: int = _ADMIN,
) -> list[IngestionRunSummary]:
    page_size = clamp_limit(limit)
    rows = ingestion_repo.list_runs(db, limit=page_size + 1, after_id=decode_cursor(cursor))
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    if has_more and rows:
        response.headers[NEXT_CURSOR_HEADER] = encode_cursor(rows[-1].id)
    return [
        IngestionRunSummary(
            id=str(r.id),
            source=r.source,
            status=r.status,
            started_at=r.started_at.isoformat() if r.started_at else None,
            ended_at=r.ended_at.isoformat() if r.ended_at else None,
        )
        for r in rows
    ]


@router.get("/eval-runs", response_model=list[EvalRunSummary])
def list_eval_runs(
    response: Response,
    limit: int | None = None,
    cursor: str | None = None,
    db: Session = _DB,
    _: int = _ADMIN,
) -> list[EvalRunSummary]:
    page_size = clamp_limit(limit)
    rows = eval_repo.list_runs(db, limit=page_size + 1, after_id=decode_cursor(cursor))
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    if has_more and rows:
        response.headers[NEXT_CURSOR_HEADER] = encode_cursor(rows[-1].id)
    return [
        EvalRunSummary(id=str(r.id), name=r.name, status=r.status, git_sha=r.git_sha) for r in rows
    ]


@router.post("/companies/{ticker}/refresh", status_code=202, response_model=RefreshAccepted)
def refresh_company(ticker: str, db: Session = _DB, _: int = _ADMIN) -> RefreshAccepted:
    """Enqueue a full re-ingest for a company; the interactive worker runs it (ADR-0017/0024)."""
    company = company_repo.get_by_ticker(db, ticker)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"company {ticker.upper()} not ingested")
    job = job_repo.create_job(db, job_type="refresh", params={"ticker": company.ticker})
    db.commit()
    if get_settings().background_jobs_enabled:
        from app.tasks.ingestion import refresh_company_data

        refresh_company_data.delay(company.ticker)
    return RefreshAccepted(ticker=company.ticker, job_id=str(job.id), status="queued")


@router.get("/research/{research_id}/rejected-claims", response_model=RejectedClaims)
def rejected_claims(research_id: int, db: Session = _DB, _: int = _ADMIN) -> RejectedClaims:
    """Counts and reasons for claims citation validation removed or softened (spec §10.16)."""
    if report_repo.get_report(db, research_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"research report {research_id} not found")
    rows = claim_repo.list_for_report(db, research_id)
    dropped = [r for r in rows if r.status != "accepted"]
    return RejectedClaims(
        research_id=str(research_id),
        checked=len(rows),
        counts=dict(Counter(r.reason_code or "UNSPECIFIED" for r in dropped)),
        claims=[
            RejectedClaim(
                claim_id=r.claim_id,
                text=r.claim_text,
                status=r.status,
                reason_code=r.reason_code,
                source_ids=list(r.source_ids or []),
                entailment_label=r.entailment_label,
                numeric_match=r.numeric_match,
            )
            for r in dropped
        ],
    )
