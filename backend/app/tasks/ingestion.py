"""Ingestion-queue tasks (JOB-003): SEC, market, news, parse, embed, metrics, EDGAR poller.

Each task is a thin, idempotent (ING-001) wrapper over a Phase 1/2 pipeline function — no new
ingestion logic. The real per-item failure isolation and `ingestion_runs`/dead-letter bookkeeping
already live in those pipelines; these tasks add the Celery queue, retry policy, and job-row
tracking (JOB-001/002).
"""

from __future__ import annotations

from typing import Any

from celery import Task

from app.comparison.peers import refresh_default_peer_set
from app.db import _session_factory, session_scope
from app.earnings.service import after_filings
from app.embeddings.pipeline import embed_all
from app.finance.builder import build_company_metrics
from app.finance.footnote_builder import build_company_footnotes
from app.finance.quality_builder import build_company_earnings_quality
from app.finance.ratio_builder import build_company_ratios
from app.finance.valuation_builder import build_company_valuation
from app.finance.valuation_history import build_valuation_history
from app.footnotes.summary import summarize_company_notes
from app.governance.builder import build_company_governance
from app.governance.facts import extract_company_facts
from app.guidance.builder import build_company_guidance
from app.guidance.extract import extract_company_guidance
from app.ingestion.documents.pipeline import parse_company_documents
from app.ingestion.news import ingest_news_for_tickers
from app.ingestion.prices import ingest_prices_for_tickers
from app.ingestion.sec import ingest_company
from app.ingestion.sec.poller import find_new_accessions
from app.redflags.detect import scan_company
from app.repositories import companies as company_repo
from app.tasks.base import RETRY_KWARGS, job_task
from app.tasks.celery_app import celery_app
from app.thesis.links import refresh_company_theses
from app.utils.storage import get_object_storage

_SEED_TICKERS = ["NVDA", "AAPL", "JPM"]


@celery_app.task(bind=True, queue="ingestion", name="fetch_sec_filings", **RETRY_KWARGS)
def fetch_sec_filings(self: Task, tickers: list[str]) -> dict[str, int]:
    storage = get_object_storage()
    out: dict[str, int] = {}
    with job_task(self, job_type="fetch_sec_filings", params={"tickers": tickers}) as (
        session,
        progress,
    ):
        for i, ticker in enumerate(tickers):
            counts = ingest_company(session, ticker, storage=storage)
            out[ticker] = counts.filings_ingested
            progress.update(progress=int((i + 1) / len(tickers) * 100), stage=f"sec:{ticker}")
    return out


@celery_app.task(bind=True, queue="ingestion", name="fetch_market_data", **RETRY_KWARGS)
def fetch_market_data(self: Task, tickers: list[str]) -> dict[str, int]:
    with job_task(self, job_type="fetch_market_data", params={"tickers": tickers}):
        results = ingest_prices_for_tickers(_session_factory(), tickers)
    return {t: c.bars for t, c in results.items()}


@celery_app.task(bind=True, queue="ingestion", name="fetch_news", **RETRY_KWARGS)
def fetch_news(self: Task, tickers: list[str]) -> dict[str, int]:
    with job_task(self, job_type="fetch_news", params={"tickers": tickers}):
        results = ingest_news_for_tickers(_session_factory(), tickers)
    return {t: c.ingested for t, c in results.items()}


@celery_app.task(bind=True, queue="ingestion", name="fetch_insider_and_holdings", **RETRY_KWARGS)
def fetch_insider_and_holdings(self: Task, tickers: list[str]) -> dict[str, Any]:
    """Form 4 / 13F ingestion is P2 and not implemented (Appendix F #10). This task exists so the
    schedule and job list are complete; it records a no-op run rather than silently missing."""
    with job_task(self, job_type="fetch_insider_and_holdings", params={"tickers": tickers}):
        pass
    return {"status": "not_implemented", "reason": "Form 4 / 13F ingestion is P2"}


@celery_app.task(bind=True, queue="ingestion", name="parse_documents", **RETRY_KWARGS)
def parse_documents(self: Task, ticker: str) -> int:
    """Parse the company's documents not yet at the current parser version (FR-EX-014)."""
    with job_task(self, job_type="parse_documents", params={"ticker": ticker}) as (
        session,
        progress,
    ):
        company = company_repo.get_by_ticker(session, ticker)
        if company is None:
            raise ValueError(f"unknown ticker: {ticker}")
        summary = parse_company_documents(session, company.id, storage=get_object_storage())
        scan_company(session, company)  # post-parse red-flag detection (FR-RF-007)
        summarize_company_notes(session, company)  # cached once per 10-K (FR-FN-009)
        build_company_governance(session, company)  # proxy pay figures (FR-GV-004)
        extract_company_facts(session, company)  # cached once per proxy (FR-GV-010)
        extract_company_guidance(session, company)  # cached once per release (FR-GD-006)
        build_company_guidance(session, company)
        refresh_company_theses(session, company)  # link new red flags (FR-TH-008)
        progress.update(progress=100, stage=f"parsed:{summary.parsed}")
    return summary.parsed


@celery_app.task(bind=True, queue="ingestion", name="generate_embeddings", **RETRY_KWARGS)
def generate_embeddings(self: Task) -> int:
    with job_task(self, job_type="generate_embeddings") as (session, _progress):
        counts = embed_all(session)
    return counts.embedded


@celery_app.task(bind=True, queue="ingestion", name="update_financial_metrics", **RETRY_KWARGS)
def update_financial_metrics(self: Task, ticker: str) -> dict[str, int]:
    with job_task(self, job_type="update_financial_metrics", params={"ticker": ticker}) as (
        session,
        _progress,
    ):
        company = company_repo.get_by_ticker(session, ticker)
        if company is None:
            raise ValueError(f"unknown ticker: {ticker}")
        base = build_company_metrics(session, company)
        ratios = build_company_ratios(session, company)
        build_company_valuation(session, company)
        build_valuation_history(session, company)  # Phase 7h bands
        refresh_default_peer_set(session, company)
        build_company_earnings_quality(session, company)
        build_company_footnotes(session, company)
        build_company_guidance(session, company)  # a new quarter's actuals (FR-GD-007)
        after_filings(session, company)  # a post-earnings brief, or its 10-Q version (FR-EB-008)
        refresh_company_theses(session, company)  # KPIs against the new figures (FR-TH-007)
    return {"metrics": base.annual_metrics, "ratios": ratios}


@celery_app.task(bind=True, queue="ingestion", name="poll_edgar_new_filings", **RETRY_KWARGS)
def poll_edgar_new_filings(self: Task, tickers: list[str] | None = None) -> dict[str, int]:
    """≤10-min EDGAR poll (§21.3): for each watched/seed company, if EDGAR has an accession we
    haven't ingested, enqueue a refresh for that company. Idempotent — the refresh re-checks."""
    tickers = tickers or _SEED_TICKERS
    new_counts: dict[str, int] = {}
    with (
        job_task(self, job_type="poll_edgar_new_filings", params={"tickers": tickers}),
        session_scope() as session,
    ):
        for ticker in tickers:
            company = company_repo.get_by_ticker(session, ticker)
            if company is None:
                continue
            new = find_new_accessions(session, company.cik)
            new_counts[ticker] = len(new)
    for ticker, n in new_counts.items():
        if n:
            refresh_company_data.delay(ticker)
    return new_counts


def _refresh_one(ticker: str) -> None:
    """Full single-company pipeline, run inline (used by the refresh task + chainable)."""
    storage = get_object_storage()
    with session_scope() as session:
        ingest_company(session, ticker, storage=storage)
    ingest_prices_for_tickers(_session_factory(), [ticker])
    ingest_news_for_tickers(_session_factory(), [ticker])
    with session_scope() as session:
        company = company_repo.get_by_ticker(session, ticker)
        if company is None:
            raise ValueError(f"unknown ticker: {ticker}")
        # Same chain as the ingest: a new earnings 8-K's EX-99 exhibits, fetched above, are
        # parsed and embedded here within one poll interval (FR-EX-015).
        parse_company_documents(session, company.id, storage=storage)
        embed_all(session)  # commits the parse too, so a 429 later in the backlog keeps both
        build_company_metrics(session, company)
        build_company_ratios(session, company)
        build_company_valuation(session, company)
        build_valuation_history(session, company)  # Phase 7h bands
        refresh_default_peer_set(session, company)
        build_company_earnings_quality(session, company)
        build_company_footnotes(session, company)
        scan_company(session, company)  # after metrics: the Beneish flag reads them (FR-RF-007)
        summarize_company_notes(session, company)  # a new 10-K's notes (FR-FN-009)
        build_company_governance(session, company)  # a new proxy (FR-GV-004)
        extract_company_facts(session, company)
        extract_company_guidance(session, company)  # a new release's outlook (FR-GD-003)
        build_company_guidance(session, company)
        after_filings(session, company)  # a post-earnings brief, or its 10-Q version (FR-EB-008)
        refresh_company_theses(session, company)  # KPIs and evidence links (FR-TH-007)


@celery_app.task(bind=True, queue="interactive", name="refresh_company_data", **RETRY_KWARGS)
def refresh_company_data(self: Task, ticker: str) -> str:
    """Admin/user-triggered full refresh for one company (the Phase 4a `POST /admin/.../refresh`
    enqueues this). On the `interactive` queue so an operator refresh isn't stuck behind the
    nightly batch."""
    with job_task(self, job_type="refresh_company_data", params={"ticker": ticker}) as (
        _session,
        progress,
    ):
        progress.update(progress=10, stage="refresh:start")
        _refresh_one(ticker)
        progress.update(progress=100, stage="refresh:done")
    return ticker


__all__ = [
    "fetch_insider_and_holdings",
    "fetch_market_data",
    "fetch_news",
    "fetch_sec_filings",
    "generate_embeddings",
    "parse_documents",
    "poll_edgar_new_filings",
    "refresh_company_data",
    "update_financial_metrics",
]
