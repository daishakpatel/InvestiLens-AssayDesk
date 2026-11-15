"""Watchlist persistence (spec §26.2). User-scoped; every query is filtered by `user_id`."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Company, Watchlist, WatchlistItem


def list_for_user(session: Session, *, user_id: int) -> list[Watchlist]:
    return list(
        session.scalars(
            select(Watchlist).where(Watchlist.user_id == user_id).order_by(Watchlist.id)
        )
    )


def company_ids_for_user(session: Session, *, user_id: int) -> list[int]:
    """Every company on any of the user's watchlists, read now (not when an alert was made)."""
    return list(
        session.scalars(
            select(WatchlistItem.company_id)
            .join(Watchlist, Watchlist.id == WatchlistItem.watchlist_id)
            .where(Watchlist.user_id == user_id)
            .distinct()
        )
    )


def item_tickers(session: Session, *, watchlist_id: int) -> list[str]:
    """Tickers on a watchlist, in insertion order (joins items → companies)."""
    return list(
        session.scalars(
            select(Company.ticker)
            .join(WatchlistItem, WatchlistItem.company_id == Company.id)
            .where(WatchlistItem.watchlist_id == watchlist_id)
            .order_by(WatchlistItem.id)
        )
    )


def create(session: Session, *, user_id: int, name: str) -> Watchlist:
    row = Watchlist(user_id=user_id, name=name)
    session.add(row)
    session.flush()
    return row


def get_for_user(session: Session, *, user_id: int, watchlist_id: int) -> Watchlist | None:
    """The watchlist if the user owns it; None for another user's or a missing one (SEC-007)."""
    row = session.get(Watchlist, watchlist_id)
    return row if row is not None and row.user_id == user_id else None


def add_item(session: Session, *, watchlist_id: int, company_id: int) -> None:
    """Add a company; already on the list is a no-op, also under concurrent adds (ADR-0062)."""
    session.execute(
        insert(WatchlistItem)
        .values(watchlist_id=watchlist_id, company_id=company_id, added_at=datetime.now(UTC))
        .on_conflict_do_nothing(index_elements=["watchlist_id", "company_id"])
    )


def remove_item(session: Session, *, watchlist_id: int, company_id: int) -> None:
    session.execute(
        delete(WatchlistItem).where(
            WatchlistItem.watchlist_id == watchlist_id, WatchlistItem.company_id == company_id
        )
    )


def delete_for_user(session: Session, *, user_id: int, watchlist_id: int) -> bool:
    """Delete a watchlist the user owns; return False if it is not theirs / not found."""
    row = session.get(Watchlist, watchlist_id)
    if row is None or row.user_id != user_id:
        return False
    session.execute(delete(Watchlist).where(Watchlist.id == watchlist_id))
    return True
