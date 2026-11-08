"""News persistence (Phase 1e). Idempotent by (company_id, content_hash)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import ColumnElement, func, select, tuple_
from sqlalchemy.orm import Session

from app.models import Document, News

# Sorts undated rows after every dated one in the newest-first list.
_UNDATED = datetime(1970, 1, 1, tzinfo=UTC)


def publisher_tiers(session: Session, document_ids: list[int]) -> dict[int, int]:
    """Map document_id → source_tier for a batch of news docs (publisher tier, §15), no N+1."""
    if not document_ids:
        return {}
    rows = session.execute(
        select(Document.id, Document.source_tier).where(Document.id.in_(document_ids))
    ).all()
    return {doc_id: tier for doc_id, tier in rows}


def list_news(
    session: Session,
    *,
    company_id: int,
    category: str | None = None,
    publisher: str | None = None,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
    min_relevance: Decimal | None = None,
    limit: int,
    after_id: int | None = None,
) -> list[News]:
    """Filtered company news, newest published first, keyset-paginated (LGL-004, §24.2).

    Providers don't deliver in time order, so ids aren't chronological: order by publish time
    (undated rows last) with id as the tie-break, and resume after the cursor row's position.
    """
    conditions: list[ColumnElement[bool]] = [News.company_id == company_id]
    if category is not None:
        conditions.append(News.category == category)
    if publisher is not None:
        conditions.append(News.publisher == publisher)
    if from_dt is not None:
        conditions.append(News.published_at >= from_dt)
    if to_dt is not None:
        conditions.append(News.published_at <= to_dt)
    if min_relevance is not None:
        conditions.append(News.relevance_score >= min_relevance)
    published = func.coalesce(News.published_at, _UNDATED)
    if after_id is not None:
        anchor = session.get(News, after_id)
        if anchor is None:  # cursor row deleted since: fall back to id order rather than restart
            conditions.append(News.id < after_id)
        else:
            anchor_at = anchor.published_at or _UNDATED
            conditions.append(tuple_(published, News.id) < tuple_(anchor_at, anchor.id))
    stmt = select(News).where(*conditions).order_by(published.desc(), News.id.desc()).limit(limit)
    return list(session.scalars(stmt))


def recent_since(days: int, *, now: datetime) -> datetime:
    """UTC cutoff `days` before `now` (kept here so the SQL boundary lives with the query)."""
    return now - timedelta(days=days)


def upsert_news(
    session: Session,
    *,
    company_id: int,
    document_id: int,
    content_hash: str,
    title: str,
    description: str,
    url: str,
    publisher: str,
    published_at: datetime,
    category: str,
    relevance_score: Decimal,
    event_cluster_id: str,
) -> bool:
    """Insert a news row, or update the mutable fields if it already exists. Returns True if new.

    Full article `content` is never stored (LGL-004, ADR-0008).
    """
    existing = session.scalar(
        select(News).where(News.company_id == company_id, News.content_hash == content_hash)
    )
    if existing is None:
        session.add(
            News(
                company_id=company_id,
                document_id=document_id,
                content_hash=content_hash,
                title=title,
                description=description,
                url=url,
                publisher=publisher,
                published_at=published_at,
                category=category,
                relevance_score=relevance_score,
                event_cluster_id=event_cluster_id,
                content=None,
            )
        )
        return True
    existing.description = description
    existing.category = category
    existing.relevance_score = relevance_score
    existing.event_cluster_id = event_cluster_id
    return False
