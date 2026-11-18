"""Batch-queue tasks (JOB-003): periodic housekeeping — alert dispatch, eval, retention purge."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from celery import Task

from app.alerts.dispatch import check_and_dispatch
from app.config import get_settings
from app.earnings.service import due_previews, try_generate
from app.models import Company
from app.notifications.sender import send_pending_emails
from app.redflags.detect import scan_company
from app.repositories import companies as company_repo
from app.repositories.chat import purge_messages_before
from app.tasks.base import RETRY_KWARGS, job_task
from app.tasks.celery_app import celery_app
from app.thesis.links import refresh_company_theses


@celery_app.task(bind=True, queue="batch", name="send_alerts", **RETRY_KWARGS)
def send_alerts(self: Task) -> int:
    """Evaluate every active alert and deliver notifications for any that fired (§26.2).

    Scheduled every few minutes so the DoD's 30-minute dispatch SLA holds even in the worst case
    (EDGAR poll ≤10 min to ingest + this to notice)."""
    with job_task(self, job_type="send_alerts") as (session, _progress):
        created = check_and_dispatch(session)
        session.commit()  # the notifications exist before any email about them goes out
        send_pending_emails(session)
        return created


@celery_app.task(bind=True, queue="batch", name="scan_red_flags", **RETRY_KWARGS)
def scan_red_flags(self: Task) -> int:
    """Nightly red-flag backfill over every active company (FR-RF-007). Idempotent: an event
    already stored under the current taxonomy version is skipped. Returns new flags stored."""
    with job_task(self, job_type="scan_red_flags") as (session, _progress):
        stored = 0
        for company in company_repo.list_active(session):
            stored += scan_company(session, company).new_flags
            refresh_company_theses(session, company)  # link new flags to theses (FR-TH-008)
        return stored


@celery_app.task(bind=True, queue="batch", name="generate_earnings_brief", **RETRY_KWARGS)
def generate_earnings_brief(
    self: Task, company_id: int, kind: Literal["preview", "brief"]
) -> int | None:
    """Make (or find unchanged) one company's preview or brief (FR-EB-008). Returns its id, or
    None when it cannot be made now (not covered, no quarter, daily cap: logged)."""
    with job_task(
        self, job_type="generate_earnings_brief", params={"company_id": company_id, "kind": kind}
    ) as (session, _progress):
        company = session.get(Company, company_id)
        if company is None:
            raise ValueError(f"unknown company id: {company_id}")
        row = try_generate(session, company, kind)
        return row.id if row else None


@celery_app.task(bind=True, queue="batch", name="generate_due_previews", **RETRY_KWARGS)
def generate_due_previews(self: Task) -> int:
    """Daily: previews for confirmed (never estimated) report dates within the lead window."""
    with job_task(self, job_type="generate_due_previews") as (session, _progress):
        return due_previews(
            session, company_repo.list_active(session), today=datetime.now(UTC).date()
        )


@celery_app.task(bind=True, queue="batch", name="purge_chat_history", **RETRY_KWARGS)
def purge_chat_history(self: Task) -> int:
    """Delete chat messages past the retention window (SEC-014)."""
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(days=settings.chat_message_retention_days)
    with job_task(self, job_type="purge_chat_history") as (session, _progress):
        return purge_messages_before(session, cutoff=cutoff)


@celery_app.task(bind=True, queue="batch", name="reset_demo_sandbox", **RETRY_KWARGS)
def reset_demo_sandbox(self: Task) -> int:
    """Nightly in demo mode: wipe the shared demo account and its data (NFR-ME-007)."""
    from app.auth.demo import reset_demo_sandbox as reset

    with job_task(self, job_type="reset_demo_sandbox") as (session, _progress):
        return int(reset(session))


@celery_app.task(bind=True, queue="batch", name="run_eval_suite", **RETRY_KWARGS)
def run_eval_suite(self: Task, *, smoke: bool = True, user_id: int = 1) -> int:
    """Nightly evaluation run (§18, Phase 5b). Persists an `eval_runs` row; returns its id.

    Deferred import of the eval stack keeps Celery worker start-up light and avoids importing the
    judge/LLM wiring in processes that never run evals.
    """
    from app.eval.dataset import load_golden
    from app.eval.judge import JUDGE_PROMPT_VERSION, FakeJudge, LLMJudge
    from app.eval.runner import config_hash, persist_run, run_eval
    from app.providers import get_llm_client

    settings = get_settings()
    judge = (
        LLMJudge(get_llm_client(), model=settings.llm_model_strong)
        if settings.provider_mode == "live"
        else FakeJudge()
    )
    smoke_path = None
    if smoke:
        from pathlib import Path

        smoke_path = Path(__file__).resolve().parents[2] / "tests" / "eval" / "smoke.jsonl"
    questions = load_golden(smoke_path)
    with job_task(self, job_type="run_eval_suite", params={"smoke": smoke}) as (session, _p):
        summary = run_eval(session, questions, user_id=user_id, llm=get_llm_client(), judge=judge)
        return persist_run(
            session,
            summary,
            name="smoke" if smoke else "full",
            config_hash=config_hash(settings, judge_version=JUDGE_PROMPT_VERSION),
            git_sha=None,
        )
