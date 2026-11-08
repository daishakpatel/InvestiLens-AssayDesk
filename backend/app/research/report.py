"""Full research-report generation (Phase 3b, §10/§17).

Converges Phase 1c metrics, Phase 1e news, Phase 2c retrieval, and Phase 3a verification into a
`ResearchReport`. Each section is an independent, verified LLM call over its own evidence subset;
a section that fails or yields zero verified claims is marked `insufficient_evidence_sections`
rather than failing the whole report. Every row records `model`, `prompt_version`, and
`data_version` (a hash of the metric + document set) for reproducibility (NFR-010).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, get_args

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.billing.budget import check_budget
from app.citation.evidence import EvidenceItem
from app.citation.pipeline import persist as persist_claims
from app.config import Settings, get_settings
from app.filings.service import change_statuses, llm_adjudicator
from app.models import Company, DocumentChunk
from app.providers import get_llm_client
from app.providers.base import LLMClient
from app.rag.retrieval import retrieve_evidence
from app.rag.types import RewrittenQuery, SearchScope
from app.repositories import companies as company_repo
from app.repositories import news as news_repo
from app.repositories import reports as report_repo
from app.repositories import users as user_repo
from app.research.evidence import from_rag
from app.research.generator import SectionGeneration, accepted_by_index, generate_section
from app.research.payloads import build_payload
from app.research.prompts import REPORT_PROMPT_VERSION
from app.schemas.citations import Citation, VerifiedOutput
from app.schemas.research import (
    Factor,
    ManagementTopicStatement,
    NewsItemSummary,
    ResearchClaim,
    ResearchReport,
    Risk,
)
from app.schemas.sources import NewsItemSource
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)

_SECTION_WORKERS = 6  # how many model calls may be in flight; the provider client paces them


class ReportGenerationError(RuntimeError):
    """No section could be generated (the model provider refused every call); nothing is saved."""


_RISK_CATEGORIES = set(get_args(Risk.model_fields["category"].annotation))
_MGMT_TOPICS = set(get_args(ManagementTopicStatement.model_fields["topic"].annotation))

_PROSE_SECTIONS = [
    "executive_summary",
    "company_overview",
    "revenue_analysis",
    "profitability_analysis",
    "balance_sheet_analysis",
    "cash_flow_analysis",
    "valuation_analysis",
]
_SECTION_QUERY: dict[str, str] = {
    "executive_summary": "overview growth profitability risk valuation",
    "company_overview": "business description segments products geography customers",
    "revenue_analysis": "revenue growth drivers segment",
    "profitability_analysis": "gross margin operating margin profitability",
    "balance_sheet_analysis": "debt liquidity working capital balance sheet",
    "cash_flow_analysis": "free cash flow capital expenditures stock-based compensation",
    "valuation_analysis": "valuation multiples",
    "risks": "risk factors supply chain competition regulation",
    "management_commentary": "management outlook guidance strategy",
    "factors": "growth risks competition outlook margins demand",
}
# Where each section's evidence comes from (ADR-0053): MD&A, risk factors or the business
# description, never proxies or press-release boilerplate. Filings before the latest 10-K are
# left out too, so a risk the company has since dropped (NVIDIA's crypto mining) is not cited.
_MDNA = frozenset({"10k_item7", "10q_part1_item2"})
_RISK = frozenset({"10k_item1a", "10q_part2_item1a"})
_BUSINESS = frozenset({"10k_item1"})
# A safe-harbor disclaimer says nothing about the business but matches "outlook" queries well.
_DISCLAIMER = re.compile(
    r"(contains?|include|includes)\s+forward-looking statements|"
    r"forward-looking statements within the meaning",
    re.I,
)
_SECTION_SCOPE: dict[str, frozenset[str]] = {
    "executive_summary": _MDNA | _BUSINESS,
    "company_overview": _BUSINESS | _MDNA,
    "revenue_analysis": _MDNA,
    "profitability_analysis": _MDNA,
    "balance_sheet_analysis": _MDNA,
    "cash_flow_analysis": _MDNA,
    "valuation_analysis": _MDNA,
    "risks": _RISK,
    "management_commentary": _MDNA,
    "factors": _RISK | _MDNA,
}


@dataclass
class ReportResult:
    report: ResearchReport
    report_id: int
    data_version: str


def _rewrite(query: str) -> RewrittenQuery:
    return RewrittenQuery(original=query, expanded=query)


def _latest_10k_date(session: Session, company_id: int) -> date | None:
    return session.scalar(
        select(func.max(DocumentChunk.filing_date)).where(
            DocumentChunk.company_id == company_id, DocumentChunk.filing_type == "10-K"
        )
    )


def _retrieve(
    session: Session,
    company_id: int,
    section: str,
    *,
    settings: Settings,
    since: date | None = None,
) -> list[EvidenceItem]:
    query = _SECTION_QUERY[section]
    scope = SearchScope(_SECTION_SCOPE[section], since)
    rewritten = _rewrite(query)
    try:
        outcome = retrieve_evidence(
            session, company_id=company_id, rewritten=rewritten, settings=settings, scope=scope
        )
        if not outcome.evidence:
            # Sections not labelled (an unusual layout): any passage beats none. Logged.
            log_event(logger, logging.WARNING, "report.retrieve.unscoped", section=section)
            outcome = retrieve_evidence(
                session, company_id=company_id, rewritten=rewritten, settings=settings
            )
    except Exception as exc:  # retrieval failure must not sink the report
        log_event(logger, logging.WARNING, "report.retrieve.failed", query=query, error=str(exc))
        return []
    return [from_rag(e) for e in outcome.evidence if not _DISCLAIMER.search(e.text)]


def _news_evidence(session: Session, company_id: int, *, settings: Settings) -> list[EvidenceItem]:
    since = news_repo.recent_since(settings.news_backfill_days, now=datetime.now(UTC))
    rows = news_repo.list_news(
        session,
        company_id=company_id,
        from_dt=since,
        min_relevance=Decimal(str(settings.news_relevance_threshold)),
        limit=20,
    )
    items: list[EvidenceItem] = []
    for row in rows:
        source = NewsItemSource(
            source_id=f"news:{row.id}",
            tier=4,
            url=row.url,
            news_id=str(row.id),
            publisher=row.publisher,
            published_at=row.published_at.isoformat() if row.published_at else None,
            excerpt_span=row.description,
        )
        text = " ".join(p for p in (row.title, row.description) if p)
        items.append(EvidenceItem(source=source, text=text, retrieval_score=0.5))
    return items


def _empty_generation() -> SectionGeneration:
    """What a section that raised becomes: empty, and marked failed rather than lacking evidence."""
    return SectionGeneration(
        items=[],
        verified=VerifiedOutput(rendered_text="", claims=[], citations=[]),
        insufficient=True,
        failed=True,
    )


def _run_section(
    llm: LLMClient,
    section: str,
    payload: dict[str, Any],
    evidence: Sequence[EvidenceItem],
    settings: Settings,
    *,
    session: Session | None,
    user_id: int | None,
) -> SectionGeneration:
    """Generate one section, never raising — a failure yields an empty (insufficient) section."""
    try:
        return generate_section(
            llm,
            section=section,
            payload=payload,
            evidence=evidence,
            settings=settings,
            session=session,
            user_id=user_id,
        )
    except Exception as exc:
        log_event(logger, logging.WARNING, "report.section.failed", section=section, error=str(exc))
        return _empty_generation()


def generate_report(
    session: Session,
    company: Company,
    *,
    llm: LLMClient | None = None,
    settings: Settings | None = None,
    supersedes_report_id: int | None = None,
    user_id: int | None = None,
) -> ReportResult:
    """Generate, verify, assemble, and persist a full report for one company (§10/§17).

    `user_id` attributes every section's LLM calls to the requesting user in `llm_calls`
    (ADR-0022) — the POST /research endpoint already checks the budget before enqueueing; this is
    the defense-in-depth check at the actual LLM-cost-incurring call site.
    """
    settings = settings or get_settings()
    llm = llm or get_llm_client()
    started = time.monotonic()
    if user_id is not None:
        user = user_repo.get_active(session, user_id)
        if user is not None:
            check_budget(session, user, settings=settings)

    sections: dict[str, SectionGeneration] = {}
    evidence_ids: set[str] = set()
    citations: dict[str, Citation] = {}
    insufficient: list[str] = []

    jobs: list[tuple[str, Sequence[EvidenceItem], dict[str, Any]]] = []

    def record(section: str, evidence: Sequence[EvidenceItem], payload: dict[str, Any]) -> None:
        evidence_ids.update(item.source_id for item in evidence)
        jobs.append((section, evidence, payload))

    since = _latest_10k_date(session, company.id)
    for section in _PROSE_SECTIONS:
        payload, metric_ev = build_payload(session, company.id, section)
        chunks = _retrieve(session, company.id, section, settings=settings, since=since)
        record(section, [*metric_ev, *chunks], payload)

    for section in ("risks", "management_commentary"):
        record(section, _retrieve(session, company.id, section, settings=settings, since=since), {})
    record("news_summary", _news_evidence(session, company.id, settings=settings), {})

    # Bull and bear draw from the SAME evidence pool so they cannot cherry-pick facts (§10.12-13).
    shared_pool = _retrieve(session, company.id, "factors", settings=settings, since=since)
    record("bull_factors", shared_pool, {})
    record("bear_factors", shared_pool, {})

    # Only the model calls overlap (NFR-003, ADR-0055): this thread leaves `session` alone until
    # they finish, and reads results in section order so a report stays reproducible (NFR-010).
    with ThreadPoolExecutor(max_workers=_SECTION_WORKERS) as pool:
        futures = [
            pool.submit(
                copy_context().run,  # keeps the request ID and trace on each section's logs
                _run_section,
                llm,
                section,
                payload,
                evidence,
                settings,
                session=session,
                user_id=user_id,
            )
            for section, evidence, payload in jobs
        ]
    for (section, _, _), future in zip(jobs, futures, strict=True):
        gen = future.result()
        sections[section] = gen
        if gen.insufficient:
            insufficient.append(section)
        for c in gen.verified.citations:
            citations.setdefault(c.source_id, c)
    if all(gen.failed for gen in sections.values()):
        # Saving an empty report as complete would replace the latest good one (ERR-002).
        log_event(logger, logging.ERROR, "report.all_sections_failed", ticker=company.ticker)
        raise ReportGenerationError(
            f"every section's model call failed for {company.ticker}; the previous report is kept"
        )

    report = _assemble(sections)
    _annotate_risk_changes(session, report.risks, llm=llm, settings=settings, user_id=user_id)
    report.insufficient_evidence_sections = sorted(set(insufficient))
    data_version = _data_version(evidence_ids)

    row = report_repo.create_report(
        session,
        company_id=company.id,
        model=settings.llm_model_strong,
        prompt_version=REPORT_PROMPT_VERSION,
        data_version=data_version,
        supersedes_report_id=supersedes_report_id,
    )
    for gen in sections.values():
        persist_claims(session, gen.verified, report_id=row.id)
    report_repo.record_sources(session, row.id, list(citations.values()))
    report_repo.finish_report(
        session,
        row,
        report_json=report.model_dump(mode="json"),
        status="complete",
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return ReportResult(report=report, report_id=row.id, data_version=data_version)


def _annotate_risk_changes(
    session: Session,
    risks: list[Risk],
    *,
    llm: LLMClient,
    settings: Settings,
    user_id: int | None,
) -> None:
    """Set each risk's `change_status` from the filing diff of its cited passage (§10.10 v2).

    A risk whose sources are not in the company's latest filing, or which has no earlier filing to
    compare against, keeps `None`: no status is better than a guessed one.
    """
    adjudicate = llm_adjudicator(llm, session=session, settings=settings, user_id=user_id)
    try:
        statuses = change_statuses(
            session,
            [sid for risk in risks for sid in risk.source_ids],
            adjudicate=adjudicate,
            settings=settings,
        )
    except Exception as exc:  # the diff is an enrichment: it must never sink the report
        log_event(logger, logging.WARNING, "report.risk_changes.failed", error=str(exc))
        return
    for risk in risks:
        risk.change_status = next(
            (statuses[sid] for sid in risk.source_ids if sid in statuses), None
        )


def _claims_from(gen: SectionGeneration) -> list[ResearchClaim]:
    return [
        ResearchClaim(
            claim_id=vc.claim_id,
            text=vc.text,
            source_ids=vc.source_ids,
            confidence_label=vc.confidence_label,
        )
        for _, vc in sorted(accepted_by_index(gen.verified).items())
        if vc.source_ids
    ]


def _assemble(sections: dict[str, SectionGeneration]) -> ResearchReport:
    return ResearchReport(
        executive_summary=_claims_from(sections["executive_summary"]),
        company_overview=_claims_from(sections["company_overview"]),
        revenue_analysis=_claims_from(sections["revenue_analysis"]),
        profitability_analysis=_claims_from(sections["profitability_analysis"]),
        balance_sheet_analysis=_claims_from(sections["balance_sheet_analysis"]),
        cash_flow_analysis=_claims_from(sections["cash_flow_analysis"]),
        valuation_analysis=_claims_from(sections["valuation_analysis"]),
        news_summary=_news_items(sections["news_summary"]),
        risks=_risks(sections["risks"]),
        management_commentary=_mgmt(sections["management_commentary"]),
        bull_factors=_factors(sections["bull_factors"]),
        bear_factors=_factors(sections["bear_factors"]),
    )


def _risks(gen: SectionGeneration) -> list[Risk]:
    out: list[Risk] = []
    for i, vc in sorted(accepted_by_index(gen.verified).items()):
        if not vc.source_ids:
            continue
        category = gen.items[i].meta.get("category", "business")
        out.append(
            Risk(
                category=category if category in _RISK_CATEGORIES else "business",
                description=vc.text,
                source_ids=vc.source_ids,
                change_status=None,
                evidence_label=vc.confidence_label,
            )
        )
    return out


def _mgmt(gen: SectionGeneration) -> list[ManagementTopicStatement]:
    out: list[ManagementTopicStatement] = []
    for i, vc in sorted(accepted_by_index(gen.verified).items()):
        meta = gen.items[i].meta
        if not vc.source_ids or meta.get("topic") not in _MGMT_TOPICS:
            continue
        out.append(
            ManagementTopicStatement(
                topic=meta["topic"],
                period=str(meta.get("period", "")),
                text=vc.text,
                speaker=meta.get("speaker"),
                source_ids=vc.source_ids,
            )
        )
    return out


def _news_items(gen: SectionGeneration) -> list[NewsItemSummary]:
    out: list[NewsItemSummary] = []
    for i, vc in sorted(accepted_by_index(gen.verified).items()):
        if not vc.source_ids:
            continue
        meta = gen.items[i].meta
        out.append(
            NewsItemSummary(
                news_id=str(meta.get("news_id", "")),
                summary=vc.text,
                category=str(meta.get("category", "general")),
                source_ids=vc.source_ids,
            )
        )
    return out


def _factors(gen: SectionGeneration) -> list[Factor]:
    grouped: dict[int, Factor] = {}
    for i, vc in sorted(accepted_by_index(gen.verified).items()):
        if not vc.source_ids:
            continue
        meta = gen.items[i].meta
        fi = int(meta.get("factor_idx", i))
        claim = ResearchClaim(
            claim_id=vc.claim_id,
            text=vc.text,
            source_ids=vc.source_ids,
            confidence_label=vc.confidence_label,
        )
        if fi in grouped:
            grouped[fi].claims.append(claim)
        else:
            grouped[fi] = Factor(
                title=str(meta.get("title") or "Factor"),
                claims=[claim],
                monitoring_indicator=meta.get("monitoring_indicator"),
            )
    return [grouped[k] for k in sorted(grouped)]


def _data_version(evidence_ids: set[str]) -> str:
    payload = json.dumps(
        {"prompt_version": REPORT_PROMPT_VERSION, "evidence": sorted(evidence_ids)}
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def generate_report_for_ticker(
    session: Session, ticker: str, *, llm: LLMClient | None = None, user_id: int | None = None
) -> ReportResult | None:
    """Job-callable entry: resolve a ticker and generate its report (Celery plumbing is Phase 5)."""
    company = company_repo.get_by_ticker(session, ticker)
    if company is None:
        return None
    return generate_report(session, company, llm=llm, user_id=user_id)
