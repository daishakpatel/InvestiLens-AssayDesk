"""Full research-report generation integration tests (Phase 3b DoD).

Real Postgres + pgvector; skips offline. Seeds a company with embedded chunks, metrics, and news,
then generates a report with a deterministic fake LLM (ADR-0009: the live path needs a key).

Refs: §10/§17, FR-020 (every claim cited), NFR-010 (reproducible), §10.12-13 (bull/bear).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.billing.budget import BudgetExceededError
from app.citation.resolver import resolve_source
from app.config import get_settings
from app.embeddings.pipeline import embed_pending
from app.models import (
    ClaimVerification,
    Company,
    Document,
    DocumentChunk,
    FinancialMetric,
    LlmCall,
    News,
    ResearchReport,
    ResearchSource,
    User,
)
from app.providers.base import LLMMessage
from app.providers.mocks.embeddings import MockEmbeddingClient
from app.repositories import reports as report_repo
from app.research.payloads import build_payload
from app.research.prompts import REPORT_PROMPT_VERSION
from app.research.report import ReportGenerationError, _news_evidence, generate_report
from app.utils.http import HttpClientError
from tests.fakes import FakeLLM, _label

_CHUNKS = [
    "Supply chain risk: reliance on a few foundry partners raised disruption exposure.",
    "Revenue grew on strong data center demand during fiscal 2025 across all regions.",
    "Gross margin expanded due to a richer mix of data-center products this year.",
    "Management expects continued revenue outlook strength and robust demand next year.",
    "The company designs GPUs for data center, gaming, and automotive markets worldwide.",
    "Competition from alternative accelerators could pressure pricing and margins over time.",
]
_REV = {2023: "26974000000", 2024: "60922000000", 2025: "130497000000"}


@pytest.fixture(scope="module")
def _migrated(test_db_url: str) -> None:
    from tests.integration.conftest import _MIGRATIONS_DIR

    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", test_db_url)
    command.upgrade(cfg, "head")


@pytest.fixture
def session(_migrated: None, test_engine: Engine) -> Iterator[Session]:
    with Session(test_engine) as s:
        yield s
        s.rollback()


def _seed_company(session: Session, ticker: str, *, gross_margin: str | None) -> Company:
    company = Company(
        ticker=ticker, cik=f"000000{ticker}"[:10].ljust(10, "0"), name=f"{ticker} Inc"
    )
    session.add(company)
    session.flush()
    doc = Document(
        company_id=company.id,
        document_type="filing",
        source_tier=1,
        content_hash=f"{ticker}-10k",
        source_url="https://sec.gov/x",
    )
    session.add(doc)
    session.flush()
    for i, body in enumerate(_CHUNKS):
        session.add(
            DocumentChunk(
                document_id=doc.id,
                company_id=company.id,
                chunk_index=i,
                chunk_type="text",
                text=body,
                section="Item 1A. Risk Factors" if i == 0 else "Item 7. MD&A",
                section_path=["Item 1A"] if i == 0 else ["Item 7"],
                paragraph_id=f"{doc.id}_{i}",
                char_start=0,
                char_end=len(body),
                tier=1,
                filing_type="10-K",
                parser_version="doc-parse-v1",
            )
        )
    for fy, value in _REV.items():
        session.add(
            FinancialMetric(
                company_id=company.id,
                period=f"FY{fy}",
                fiscal_year=fy,
                period_type="FY",
                metric_name="revenue",
                metric_value=Decimal(value),
                unit="USD",
                source_id=f"xbrl:{ticker}:revenue:FY{fy}",
                is_latest=True,
            )
        )
    session.add(
        FinancialMetric(
            company_id=company.id,
            period="FY2025",
            fiscal_year=2025,
            period_type="FY",
            metric_name="gross_margin",
            metric_value=Decimal(gross_margin) if gross_margin is not None else None,
            unit="ratio",
            is_derived=True,
            formula_id="gross_margin",
            source_id="derived:gross_margin:FY2025",
            is_latest=True,
            quality_flags={
                "inputs": [],
                "formula_version": "v1",
                "warnings": [] if gross_margin else ["NOT_APPLICABLE_SECTOR"],
            },
        )
    )
    session.add(
        News(
            company_id=company.id,
            title="Analyst upgrade on AI demand",
            description="Data center momentum cited.",
            url="https://news/1",
            publisher="Reuters",
            published_at=datetime.now(UTC),
            relevance_score=Decimal("0.9"),
        )
    )
    session.flush()
    embed_pending(session, client=MockEmbeddingClient())
    return company


def test_news_evidence_leaves_out_passing_mentions(session: Session) -> None:
    """§10.8, FR-020: a report summarizes the same news the News tab shows, not market stories."""
    company = _seed_company(session, "NWS", gross_margin="0.70")
    session.add(
        News(
            company_id=company.id,
            title="Treasury market suffers a historic sell-off",
            url="https://news/2",
            publisher="Reuters",
            published_at=datetime.now(UTC),
            relevance_score=Decimal("0.1"),
        )
    )
    session.flush()
    evidence = _news_evidence(session, company.id, settings=get_settings())
    assert [item.text.split(" Data center")[0] for item in evidence] == [
        "Analyst upgrade on AI demand"
    ]


def test_full_report_generates_and_persists(session: Session) -> None:
    company = _seed_company(session, "TST", gross_margin="0.71")
    result = generate_report(session, company, llm=FakeLLM())

    report = result.report
    populated = report.revenue_analysis + report.risks + report.company_overview
    assert populated, "expected at least some verified sections"
    # FR-020: every rendered claim carries a verified citation.
    for claim in report.executive_summary + report.revenue_analysis + report.company_overview:
        assert claim.source_ids
    for risk in report.risks:
        assert risk.source_ids  # §10.10: a risk with no citation is rejected by Phase 3a

    row = session.get(ResearchReport, result.report_id)
    assert row is not None
    assert row.prompt_version == REPORT_PROMPT_VERSION and row.model and row.data_version
    assert row.status == "complete" and row.report_json is not None

    assert session.scalar(
        select(func.count()).select_from(ResearchSource).where(ResearchSource.report_id == row.id)
    )
    assert session.scalar(
        select(func.count())
        .select_from(ClaimVerification)
        .where(ClaimVerification.report_id == row.id)
    )


class _OverlapLLM(FakeLLM):
    """The first call waits for a second to arrive, which only happens if sections overlap."""

    def __init__(self) -> None:
        super().__init__()
        self._second = threading.Event()
        self._lock = threading.Lock()
        self._calls = 0
        self.overlapped = False

    async def complete_json(
        self, messages: Sequence[LLMMessage], *, model: str, schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        with self._lock:
            self._calls += 1
            first = self._calls == 1
        if first:
            self.overlapped = self._second.wait(timeout=5)
        else:
            self._second.set()
        return await super().complete_json(
            messages, model=model, schema=schema, max_tokens=max_tokens
        )


def test_sections_generate_concurrently_and_keep_their_order(session: Session) -> None:
    """NFR-003, NFR-010: overlapping model calls, while the report and its logs stay the same."""
    company = _seed_company(session, "PAR", gross_margin="0.71")
    user = User(email="par@example.com", password_hash="x", is_active=True)  # noqa: S106
    session.add(user)
    session.flush()

    llm = _OverlapLLM()
    overlapped = generate_report(session, company, llm=llm, user_id=user.id)
    in_order = generate_report(session, company, llm=FakeLLM())

    assert llm.overlapped
    assert overlapped.report.model_dump() == in_order.report.model_dump()
    assert overlapped.data_version == in_order.data_version
    assert session.scalar(
        select(func.count())
        .select_from(LlmCall)
        .where(LlmCall.purpose == "report_section", LlmCall.user_id == user.id)
    )


def test_revenue_growth_citations_resolve(session: Session) -> None:
    """CIT-003, CIT-006: a growth figure a report cites opens with its formula and inputs."""
    company = _seed_company(session, "GRW", gross_margin="0.70")
    _, evidence = build_payload(session, company.id, "revenue_analysis")
    growth = {e.source_id: e for e in evidence if ":revenue_" in e.source_id}
    assert set(growth) == {
        "derived:GRW:revenue_yoy:FY2024-FY2025",
        "derived:GRW:revenue_cagr:FY2023-FY2025",
    }
    for source_id, item in growth.items():
        detail = resolve_source(session, source_id)
        assert detail is not None and detail.source.source_type == "derived_metric"
        assert detail.source.value == item.source.value  # type: ignore[union-attr]
        assert detail.source.input_source_ids == item.source.input_source_ids  # type: ignore[union-attr]
        assert len(detail.lineage) == 2


def test_report_generation_logs_every_llm_call(session: Session) -> None:
    """Observability audit (OBS-005): report-section LLM calls must log to `llm_calls` like
    chat/embeddings do — this was the gap the Phase 5c audit found (app.research.generator
    called the LLM directly with no `llm_calls` row)."""
    company = _seed_company(session, "OBS", gross_margin="0.71")
    user = User(email="obs@example.com", password_hash="x", is_active=True)  # noqa: S106
    session.add(user)
    session.flush()

    generate_report(session, company, llm=FakeLLM(), user_id=user.id)

    rows = session.scalars(
        select(LlmCall).where(LlmCall.purpose == "report_section", LlmCall.user_id == user.id)
    ).all()
    assert rows, "expected at least one report_section llm_calls row"
    for row in rows:
        assert row.prompt_version == REPORT_PROMPT_VERSION
        assert row.model
        assert row.cost_usd is not None
        assert row.latency_ms is not None


class _RefusingLLM(FakeLLM):
    """Every call raises, as Gemini's 402 did once the prepaid credit ran out (2026-11-03)."""

    async def complete_json(
        self, messages: Sequence[LLMMessage], *, model: str, schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        raise HttpClientError("402 Payment Required")


def test_report_whose_model_calls_all_fail_is_not_saved(session: Session) -> None:
    """ERR-002, FR-030: an outage must not replace the latest good report with an empty one."""
    company = _seed_company(session, "OUT", gross_margin="0.71")
    good = generate_report(session, company, llm=FakeLLM())
    reports = (
        select(func.count())
        .select_from(ResearchReport)
        .where(ResearchReport.company_id == company.id)
    )
    before = session.scalar(reports)

    with pytest.raises(ReportGenerationError):
        generate_report(session, company, llm=_RefusingLLM())

    assert session.scalar(reports) == before
    latest = report_repo.get_latest_complete(session, company_id=company.id)
    assert latest is not None and latest.id == good.report_id


class _PartlyRefusingLLM(FakeLLM):
    """Raises for the named sections only, as when the credit ran out mid-report (report 36)."""

    def __init__(self, refused: set[str]) -> None:
        super().__init__()
        self._refused = refused

    async def complete_json(
        self, messages: Sequence[LLMMessage], *, model: str, schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        if _label(messages[0].content) in self._refused:
            raise HttpClientError("402 Payment Required")
        return await super().complete_json(
            messages, model=model, schema=schema, max_tokens=max_tokens
        )


def test_failed_section_is_carried_over_from_the_previous_report(session: Session) -> None:
    """ERR-002, FR-030 (ADR-0057): a section lost to an outage keeps the last report's version,
    labelled with that report, and is not reported as lacking evidence."""
    company = _seed_company(session, "CAR", gross_margin="0.71")
    good = generate_report(session, company, llm=FakeLLM())
    assert good.report.risks

    partial = generate_report(session, company, llm=_PartlyRefusingLLM({"risks"}))

    report = partial.report
    assert report.risks == good.report.risks
    assert report.carried_over is not None
    assert report.carried_over.research_id == str(good.report_id)
    assert report.carried_over.sections == ["risks"]
    assert report.failed_sections == []
    assert "risks" not in report.insufficient_evidence_sections
    assert report.company_overview  # the sections that did generate are this run's
    stored = set(
        session.scalars(
            select(ResearchSource.source_id).where(ResearchSource.report_id == partial.report_id)
        )
    )
    assert {sid for risk in report.risks for sid in risk.source_ids} <= stored

    # Carried content is not carried again: its date would then be wrong.
    again = generate_report(session, company, llm=_PartlyRefusingLLM({"risks"}))
    assert again.report.failed_sections == ["risks"] and again.report.risks == []


def test_failed_section_without_a_previous_report_is_named_failed(session: Session) -> None:
    """ERR-002 (ADR-0057): with nothing to carry over, an outage is named, not called abstention."""
    company = _seed_company(session, "FST", gross_margin="0.71")
    result = generate_report(session, company, llm=_PartlyRefusingLLM({"risks"}))
    assert result.report.failed_sections == ["risks"]
    assert result.report.carried_over is None
    assert "risks" not in result.report.insufficient_evidence_sections
    row = session.get(ResearchReport, result.report_id)
    assert row is not None and row.status == "complete"


def test_report_generation_blocked_over_ai_budget(session: Session) -> None:
    """NFR-008/ADR-0022: a user who has already spent their monthly AI budget is blocked before
    another expensive report generation runs, with a clear (not generic) error."""
    company = _seed_company(session, "BUD", gross_margin="0.71")
    user = User(
        email="budget@example.com",
        password_hash="x",  # noqa: S106
        is_active=True,
        ai_budget_month_usd=Decimal("1.00"),
    )
    session.add(user)
    session.flush()
    session.add(
        LlmCall(
            user_id=user.id,
            purpose="chat",
            model="claude-sonnet-5-5",
            input_tokens=1,
            cost_usd=Decimal("1.00"),
            latency_ms=1,
            status="success",
        )
    )
    session.flush()

    with pytest.raises(BudgetExceededError):
        generate_report(session, company, llm=FakeLLM(), user_id=user.id)


def test_report_is_reproducible(session: Session) -> None:
    company = _seed_company(session, "REP", gross_margin="0.71")
    first = generate_report(session, company, llm=FakeLLM())
    second = generate_report(session, company, llm=FakeLLM())
    assert first.data_version == second.data_version
    assert first.report.model_dump(mode="json") == second.report.model_dump(mode="json")


def test_section_failure_isolated(session: Session) -> None:
    company = _seed_company(session, "ISO", gross_margin="0.71")
    result = generate_report(session, company, llm=FakeLLM(broken={"risks"}))
    assert "risks" in result.report.insufficient_evidence_sections
    assert result.report.risks == []
    # The rest of the report still generated — one thin section never sinks the whole report.
    assert result.report.revenue_analysis or result.report.company_overview
    assert session.get(ResearchReport, result.report_id) is not None


def test_bull_bear_share_evidence(session: Session) -> None:
    company = _seed_company(session, "BB", gross_margin="0.71")
    result = generate_report(session, company, llm=FakeLLM())
    bull_ids = {sid for f in result.report.bull_factors for c in f.claims for sid in c.source_ids}
    bear_ids = {sid for f in result.report.bear_factors for c in f.claims for sid in c.source_ids}
    if bull_ids and bear_ids:
        # Both drew from the same evidence pool, so they cite the same underlying facts (§10.12-13).
        assert bull_ids & bear_ids


def test_jpm_inapplicable_metric_labeled_na(session: Session) -> None:
    company = _seed_company(session, "JPM", gross_margin=None)  # bank: gross_margin is N/A
    payload, _ev = build_payload(session, company.id, "profitability_analysis")
    block = payload["metrics"]["gross_margin"]
    # Labeled with the reason, never a zero or a blank (DR-001, DR-041/042) — said once, not as a
    # year-by-year series for the model to narrate.
    assert block == {"not_applicable": "not applicable for this sector"}


def test_payload_hands_the_model_readable_figures(session: Session) -> None:
    # FR-027: the model quotes "$130.5 billion" instead of copying a raw Decimal, and the evidence
    # it cites reads the same way, so the verifier still matches the stored value.
    company = _seed_company(session, "NVDA", gross_margin="0.7500")
    payload, evidence = build_payload(session, company.id, "revenue_analysis")
    revenue = payload["metrics"]["revenue"]
    assert revenue["series"]["FY2025"] == "$130.5 billion"
    assert revenue["calculated"]["yoy"]["value"] == "114.2%"  # 1.14204…, trailing zero dropped
    assert "Revenue for FY2025 was $130.5 billion" in [e.text for e in evidence]


def test_payload_covers_the_last_five_fiscal_years_and_skips_empty_metrics(
    session: Session,
) -> None:
    # A report reads like NVDA's when the model gets recent history, not two decades of it, and is
    # not asked to narrate a metric that has no value in any year.
    company = _seed_company(session, "TST", gross_margin="0.7500")
    for fy in range(2016, 2023):  # seven more years of revenue before the seeded 2023-2025
        session.add(
            FinancialMetric(
                company_id=company.id,
                period=f"FY{fy}",
                fiscal_year=fy,
                period_type="FY",
                metric_name="revenue",
                metric_value=Decimal(10**9 * fy),
                unit="USD",
                source_id=f"xbrl:TST:revenue:FY{fy}",
                is_latest=True,
            )
        )
    session.add(
        FinancialMetric(
            company_id=company.id,
            period="FY2025",
            fiscal_year=2025,
            period_type="FY",
            metric_name="operating_margin",
            metric_value=None,
            unit="ratio",
            is_derived=True,
            source_id="derived:TST:operating_margin:FY2025",
            is_latest=True,
            quality_flags={"warnings": ["MISSING_INPUT"]},
        )
    )
    session.flush()
    revenue, _ = build_payload(session, company.id, "revenue_analysis")
    assert list(revenue["metrics"]["revenue"]["series"]) == [f"FY{y}" for y in range(2021, 2026)]
    profitability, _ = build_payload(session, company.id, "profitability_analysis")
    assert "operating_margin" not in profitability["metrics"]  # no value in any year
