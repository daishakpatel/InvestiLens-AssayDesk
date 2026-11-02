"""Price history + corporate action persistence (Phase 1d).

`price_history` upserts by (company, date) so a daily refresh is idempotent (ING-001).
`corporate_actions` are replaced per company+provider on each run (idempotent; the provider is
the source of truth for splits/dividends, DR-025).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import CorporateAction, PriceHistory


def get_price_history(
    session: Session, *, company_id: int, start: date, end: date
) -> list[PriceHistory]:
    """Daily bars in [start, end], oldest first, for the stock-history tool (RAG-031)."""
    return list(
        session.scalars(
            select(PriceHistory)
            .where(
                PriceHistory.company_id == company_id,
                PriceHistory.date >= start,
                PriceHistory.date <= end,
            )
            .order_by(PriceHistory.date)
        )
    )


def bar_on_or_before(session: Session, *, company_id: int, day: date) -> PriceHistory | None:
    """The trading day's bar, or the last one before it (a weekend or holiday), or None."""
    return session.scalar(
        select(PriceHistory)
        .where(PriceHistory.company_id == company_id, PriceHistory.date <= day)
        .order_by(PriceHistory.date.desc())
        .limit(1)
    )


def upsert_price_bars(session: Session, company_id: int, bars: list[dict[str, Any]]) -> int:
    """Upsert daily bars by (company_id, date). Returns the number of rows written."""
    if not bars:
        return 0
    stmt = pg_insert(PriceHistory)
    update_cols = {
        c: stmt.excluded[c]
        for c in ("open", "high", "low", "close", "adj_close", "volume", "provider", "ingested_at")
    }
    stmt = stmt.values(bars).on_conflict_do_update(
        index_elements=[PriceHistory.company_id, PriceHistory.date], set_=update_cols
    )
    session.execute(stmt)
    return len(bars)


def drop_other_providers(session: Session, company_id: int, *, keep: str) -> None:
    """Remove a company's bars and corporate actions that came from any provider but `keep`.

    Sample and real prices must not share a history: the sample set has a bar on every weekday,
    so an upsert alone would leave made-up bars on market holidays between the real ones.
    """
    for model in (PriceHistory, CorporateAction):
        session.execute(delete(model).where(model.company_id == company_id, model.provider != keep))


def replace_corporate_actions(
    session: Session, company_id: int, actions: list[dict[str, Any]], *, provider: str
) -> int:
    """Replace this provider's corporate actions for a company (idempotent)."""
    session.execute(
        delete(CorporateAction).where(
            CorporateAction.company_id == company_id, CorporateAction.provider == provider
        )
    )
    if actions:
        session.bulk_insert_mappings(CorporateAction, actions)
    return len(actions)


def price_bar_mapping(
    company_id: int,
    *,
    bar_date: date,
    open_: Decimal | None,
    high: Decimal | None,
    low: Decimal | None,
    close: Decimal | None,
    adj_close: Decimal | None,
    volume: int | None,
    provider: str,
    ingested_at: datetime,
) -> dict[str, Any]:
    return {
        "company_id": company_id,
        "date": bar_date,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "adj_close": adj_close,
        "volume": volume,
        "provider": provider,
        "ingested_at": ingested_at,
    }
