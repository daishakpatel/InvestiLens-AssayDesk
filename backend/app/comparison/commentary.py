"""AI comparison commentary — cited differences only, verified by the Phase 3a pipeline (§37.1).

This builds no numbers: it hands the LLM the deterministic, calendarized metric values as evidence,
asks for prose describing the *differences*, then runs the exact same `citation.verify_text`
pipeline that reports and chat use. Unsupported claims are dropped by the verifier; a recommendation
that slips through is caught by the non-advice guard (LGL-006) and the whole commentary is withheld.
"""

from __future__ import annotations

import asyncio
import time

from sqlalchemy.orm import Session

from app.billing.cost import estimate_cost
from app.citation.advice import contains_advice
from app.citation.evidence import EvidenceItem
from app.citation.markdown import plain_text
from app.citation.pipeline import verify_text
from app.comparison.engine import GatheredComparison
from app.config import Settings, get_settings
from app.ingestion.documents.tokens import estimate_tokens
from app.models import FinancialMetric
from app.providers import get_llm_client
from app.providers.base import LLMClient, LLMMessage
from app.repositories import llm_calls as llm_repo
from app.research.evidence import from_metric
from app.schemas.citations import VerifiedOutput

_SYSTEM_RULES = (
    "You are a financial-analysis assistant comparing public companies. Use ONLY the numeric "
    "evidence provided; never invent or compute numbers. Describe the notable DIFFERENCES between "
    "the companies. Cite EVERY factual sentence with [SOURCE:id] using the ids given. Write plain "
    "sentences in short paragraphs; do not use Markdown (no headings, bullet points or bold). Use "
    "neutral, hedged language. You MUST NOT give any buy/sell/hold recommendation, price "
    "target, or "
    "investment advice — this is analysis only (LGL-006)."
)

_EMPTY = VerifiedOutput(rendered_text="", claims=[], citations=[], sufficient=False)


def _label_text(company_ticker: str, metric: FinancialMetric, calendar_year: int | None) -> str:
    base = from_metric(metric).text  # "<metric> <period> = <value> <unit>"
    cy = f" (calendar year {calendar_year})" if calendar_year is not None else ""
    return f"{company_ticker}: {base}{cy}"


def build_evidence(g: GatheredComparison) -> list[EvidenceItem]:
    """One evidence item per (company, metric) cell that has a value, with a unique source id."""
    items: list[EvidenceItem] = []
    for company in g.companies:
        for metric_name in g.metric_names:
            row = g.selected(company, metric_name)
            if row is None or row.metric_value is None:
                continue
            base = from_metric(row)
            items.append(
                EvidenceItem(
                    source=base.source,
                    text=_label_text(company.ticker, row, g.target_year),
                    retrieval_score=base.retrieval_score,
                )
            )
    return items


def _messages(g: GatheredComparison, evidence: list[EvidenceItem]) -> list[LLMMessage]:
    tickers = " vs ".join(c.ticker for c in g.companies)
    header = f"Compare {tickers} for calendar year {g.target_year}. Describe the key differences."
    lines = [header, "", "EVIDENCE (cite by source_id):"]
    for item in evidence:
        lines.append(f"[source_id={item.source_id}]\n{item.searchable_text()}")
    return [LLMMessage("system", _SYSTEM_RULES), LLMMessage("user", "\n\n".join(lines))]


def generate_commentary(
    session: Session,
    g: GatheredComparison,
    *,
    llm: LLMClient | None = None,
    settings: Settings | None = None,
    user_id: int | None = None,
) -> VerifiedOutput:
    """Generate + verify comparison commentary. Returns an empty/insufficient output when it cannot
    be supported or if any advice language is detected."""
    settings = settings or get_settings()
    evidence = build_evidence(g)
    if not evidence:
        return _EMPTY
    llm = llm or get_llm_client()
    messages = _messages(g, evidence)
    model = settings.llm_model_strong
    started = time.monotonic()
    prose = plain_text(asyncio.run(llm.complete(messages, model=model, max_tokens=600)))
    # Logged like every model call, so it counts toward the caller's AI budget (ADR-0022).
    tokens_in, tokens_out = llm_repo.call_tokens(
        llm,
        input_estimate=sum(estimate_tokens(m.content) for m in messages),
        output_estimate=estimate_tokens(prose),
    )
    llm_repo.record_call(
        session,
        purpose="comparison_commentary",
        model=model,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        latency_ms=int((time.monotonic() - started) * 1000),
        cost_usd=estimate_cost(model=model, input_tokens=tokens_in, output_tokens=tokens_out or 0),
        status="success",
        user_id=user_id,
    )
    verified = verify_text(prose, evidence, settings=settings)
    if contains_advice(verified.rendered_text):
        # Never surface a recommendation, even a verified one (LGL-006).
        return _EMPTY
    return verified
