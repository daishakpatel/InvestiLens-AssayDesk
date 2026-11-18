"""The alert event types that existed before Phase 7a, registered through the registry with the
same triggers as the Phase 5d/6b dispatch (FR-AL-003).

Event time for filings and news is the *ingestion* time (`created_at`) and for prices the bar's
`ingested_at`, when the bar was first stored (a re-fetch keeps it): the 30-minute SLA is measured
from when Assay Desk learns of an event, which the EDGAR poller (≤10 min) and the 5-minute
dispatch together bound.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alerts.registry import (
    AlertEventType,
    DetectedEvent,
    NoParams,
    register,
    scope_company_ids,
)
from app.filings import service as diff_service
from app.models import Alert, Company, Filing, News, PriceHistory
from app.repositories.companies import tickers_by_id

_SAMPLE_AT = datetime(2026, 1, 15, 21, 0, tzinfo=UTC)


# --- new_filing ---------------------------------------------------------------------------------

FormType = Literal["10-K", "10-Q", "8-K"]
_ALL_FORMS: list[FormType] = ["10-K", "10-Q", "8-K"]


class NewFilingParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    form_types: list[FormType] = Field(
        default=_ALL_FORMS,  # pydantic copies mutable defaults per instance
        min_length=1,
        title="Forms",
        description="Which SEC forms to be told about.",
    )


def _filing_event(
    *, filing_id: int, company_id: int, ticker: str, form: str, accession: str, at: datetime
) -> DetectedEvent:
    return DetectedEvent(
        event_key=f"new_filing:{ticker}:{accession}",
        occurred_at=at,
        company_id=company_id,
        title=f"New {form} for {ticker}",
        body=f"{ticker} filed a {form} ({accession}).",
        source_ids=(f"filing:{filing_id}",),
    )


def detect_new_filings(
    session: Session, alert: Alert, since: datetime, until: datetime
) -> list[DetectedEvent]:
    params = NewFilingParams.model_validate(alert.params)
    rows = session.execute(
        select(Filing, Company.ticker)
        .join(Company, Company.id == Filing.company_id)
        .where(
            Filing.company_id.in_(scope_company_ids(session, alert)),
            Filing.filing_type.in_(params.form_types),
            Filing.created_at > since,
            Filing.created_at <= until,
        )
        .order_by(Filing.created_at, Filing.id)
    )
    return [
        _filing_event(
            filing_id=filing.id,
            company_id=filing.company_id,
            ticker=ticker,
            form=filing.filing_type,
            accession=filing.accession_number,
            at=filing.created_at,
        )
        for filing, ticker in rows
    ]


# --- price_move ---------------------------------------------------------------------------------


class PriceMoveParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A ratio, not money: 0.05 = a 5% close-to-close move (DR ratios-as-decimals convention).
    threshold: float = Field(
        default=0.05,
        gt=0,
        le=1,
        title="Move threshold",
        description="Smallest one-day close-to-close move that fires, as a fraction (0.05 = 5%).",
        json_schema_extra={"x-unit": "ratio"},  # the settings form shows and takes it as a percent
    )


def _price_event(
    *,
    company_id: int,
    ticker: str,
    prev_date: date,
    bar_date: date,
    move: Decimal,
    threshold: float,
    at: datetime,
) -> DetectedEvent:
    return DetectedEvent(
        event_key=f"price_move:{ticker}:{bar_date.isoformat()}",
        occurred_at=at,
        company_id=company_id,
        title=f"{ticker} moved {move:+.1%}",
        body=(
            f"{ticker} closed {move:+.1%} on {bar_date.isoformat()} vs the prior session "
            f"(threshold {threshold:.0%})."
        ),
        source_ids=(f"price:{ticker}:{prev_date.isoformat()}:{bar_date.isoformat()}",),
        severity="warning",
    )


def detect_price_moves(
    session: Session, alert: Alert, since: datetime, until: datetime
) -> list[DetectedEvent]:
    """Fire for each daily bar first stored inside the window that closed at least `threshold`
    away from the bar before it, so a run that catches up several days checks every one of them.

    Only bars dated after the newest bar stored before the window count (a longer backfill of
    old dates is history, not news), and a company's first load counts only its newest bar."""
    params = PriceMoveParams.model_validate(alert.params)
    company_ids = scope_company_ids(session, alert)
    company, day = PriceHistory.company_id, PriceHistory.date
    lagged = (
        select(
            PriceHistory.company_id,
            PriceHistory.date,
            PriceHistory.close,
            PriceHistory.ingested_at,
            func.lag(PriceHistory.date).over(partition_by=company, order_by=day).label("prev_date"),
            func.lag(PriceHistory.close)
            .over(partition_by=company, order_by=day)
            .label("prev_close"),
        )
        .where(PriceHistory.company_id.in_(company_ids))
        .subquery()
    )
    known = (
        select(PriceHistory.company_id, func.max(PriceHistory.date).label("last_date"))
        .where(PriceHistory.company_id.in_(company_ids), PriceHistory.ingested_at <= since)
        .group_by(PriceHistory.company_id)
        .subquery()
    )
    rows = session.execute(
        select(lagged, known.c.last_date)
        .outerjoin(known, known.c.company_id == lagged.c.company_id)
        .where(lagged.c.ingested_at > since, lagged.c.ingested_at <= until)
        .where((known.c.last_date.is_(None)) | (lagged.c.date > known.c.last_date))
        .order_by(lagged.c.company_id, lagged.c.date)
    ).all()
    new_bars: dict[int, list[Any]] = {}
    for row in rows:
        new_bars.setdefault(row.company_id, []).append(row)

    tickers = tickers_by_id(session, new_bars)
    threshold = Decimal(str(params.threshold))
    events: list[DetectedEvent] = []
    for company_id, bars in new_bars.items():
        if bars[0].last_date is None:  # first load: years of history arrive at once
            bars = bars[-1:]
        for bar in bars:
            if bar.close is None or not bar.prev_close:
                continue
            move = (bar.close - bar.prev_close) / bar.prev_close
            if abs(move) >= threshold:
                events.append(
                    _price_event(
                        company_id=company_id,
                        ticker=tickers[company_id],
                        prev_date=bar.prev_date,
                        bar_date=bar.date,
                        move=move,
                        threshold=params.threshold,
                        at=bar.ingested_at,
                    )
                )
    return events


# --- new_risk_factor (§37.4) --------------------------------------------------------------------


def _risk_event(
    *,
    filing_id: int,
    company_id: int,
    ticker: str,
    form: str,
    accession: str,
    headings: list[str],
    unit_source_ids: tuple[str, ...],
    at: datetime,
) -> DetectedEvent:
    return DetectedEvent(
        event_key=f"new_risk_factor:{ticker}:{accession}",
        occurred_at=at,
        company_id=company_id,
        title=f"New risk factor in {ticker} {form}",
        body=(
            f"{len(headings)} risk factor(s) in {accession} were not in the prior {form}. "
            f"First: “{headings[0][:200]}”"
        ),
        source_ids=(f"filing:{filing_id}", *unit_source_ids),
        severity="warning",
    )


def detect_new_risk_factors(
    session: Session, alert: Alert, since: datetime, until: datetime
) -> list[DetectedEvent]:
    """Fire when a company's newest parsed 10-K/10-Q, ingested inside the window, adds a risk
    factor its predecessor did not have. Deterministic diff only: no LLM in the alert loop."""
    company_ids = scope_company_ids(session, alert)
    tickers = tickers_by_id(session, company_ids)
    events: list[DetectedEvent] = []
    for company_id in company_ids:
        for form in ("10-K", "10-Q"):
            pair = diff_service.latest_pair(session, company_id=company_id, filing_type=form)
            if pair is None or not since < pair[0].created_at <= until:
                continue
            latest, previous = pair
            added = [
                item.new
                for item in diff_service.diff_filings(session, latest, previous).items
                if item.status == "new" and item.new is not None and item.new.kind == "risk_factor"
            ]
            if added:
                events.append(
                    _risk_event(
                        filing_id=latest.id,
                        company_id=company_id,
                        ticker=tickers[company_id],
                        form=form,
                        accession=latest.accession_number,
                        headings=[unit.heading for unit in added],
                        unit_source_ids=added[0].source_ids,
                        at=latest.created_at,
                    )
                )
    return events


# --- news_category ------------------------------------------------------------------------------


class NewsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str | None = Field(
        default=None,
        max_length=64,
        title="Category",
        description="Only news in this category. Leave empty for all news.",
    )


def _news_event(
    *, news_id: int, company_id: int, ticker: str, category: str | None, headline: str, at: datetime
) -> DetectedEvent:
    return DetectedEvent(
        event_key=f"news_category:{ticker}:{news_id}",
        occurred_at=at,
        company_id=company_id,
        title=f"New {category or 'news'} news for {ticker}"[:256],
        body=headline,
        source_ids=(f"news:{news_id}",),
    )


def detect_news(
    session: Session, alert: Alert, since: datetime, until: datetime
) -> list[DetectedEvent]:
    params = NewsParams.model_validate(alert.params)
    conditions = [
        News.company_id.in_(scope_company_ids(session, alert)),
        News.created_at > since,
        News.created_at <= until,
    ]
    if params.category:
        conditions.append(News.category == params.category)
    rows = session.execute(
        select(News, Company.ticker)
        .join(Company, Company.id == News.company_id)
        .where(*conditions)
        .order_by(News.created_at, News.id)
    )
    return [
        _news_event(
            news_id=news.id,
            company_id=news.company_id,
            ticker=ticker,
            category=params.category,
            headline=news.title,
            at=news.created_at,
        )
        for news, ticker in rows
    ]


register(
    AlertEventType(
        key="new_filing",
        label="New filing",
        description="The company files a new 10-K, 10-Q or 8-K with the SEC.",
        params_schema=NewFilingParams,
        detector=detect_new_filings,
        sample=_filing_event(
            filing_id=1,
            company_id=1,
            ticker="NVDA",
            form="8-K",
            accession="0001045810-26-000001",
            at=_SAMPLE_AT,
        ),
    )
)
register(
    AlertEventType(
        key="new_risk_factor",
        label="New risk factor",
        description="A new 10-K or 10-Q adds a risk factor the previous one did not have.",
        params_schema=NoParams,
        detector=detect_new_risk_factors,
        sample=_risk_event(
            filing_id=1,
            company_id=1,
            ticker="NVDA",
            form="10-K",
            accession="0001045810-26-000002",
            headings=["Export licences are now required for our fastest chips."],
            unit_source_ids=("1_12",),
            at=_SAMPLE_AT,
        ),
    )
)
register(
    AlertEventType(
        key="price_move",
        label="Price move",
        description="A one-day close-to-close move larger than your threshold.",
        params_schema=PriceMoveParams,
        detector=detect_price_moves,
        sample=_price_event(
            company_id=1,
            ticker="NVDA",
            prev_date=date(2026, 1, 14),
            bar_date=date(2026, 1, 15),
            move=Decimal("-0.0712"),
            threshold=0.05,
            at=_SAMPLE_AT,
        ),
    )
)
register(
    AlertEventType(
        key="news_category",
        label="News",
        description="New coverage of the company, optionally limited to one category.",
        params_schema=NewsParams,
        detector=detect_news,
        sample=_news_event(
            news_id=1,
            company_id=1,
            ticker="NVDA",
            category="earnings",
            headline="NVIDIA reports fourth-quarter results",
            at=_SAMPLE_AT,
        ),
    )
)
