"""chat_sessions + chat_messages persistence (Phase 3c, §10.14).

A session belongs to a user and a company (ADR-0006: chat requires an account). Messages store the
answer's cited source IDs, the tool trace, cost/latency, and per-message feedback for the Phase 5b
golden-set loop.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.orm import Session

from app.models import ChatMessage, ChatSession, Company


def create_session(session: Session, *, user_id: int, company_id: int) -> ChatSession:
    row = ChatSession(user_id=user_id, company_id=company_id)
    session.add(row)
    session.flush()
    return row


def get_session(session: Session, session_id: int) -> ChatSession | None:
    return session.get(ChatSession, session_id)


def recent_messages(session: Session, *, session_id: int, limit: int) -> list[ChatMessage]:
    """The last `limit` messages for a session, oldest first (bounded memory window)."""
    rows = list(
        session.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id.desc())
            .limit(limit)
        )
    )
    return list(reversed(rows))


def questions_for_user(
    session: Session, *, user_id: int, limit: int, before_id: int | None
) -> list[tuple[ChatMessage, str]]:
    """A reader's own questions with their tickers, newest first, keyset by message id (§26.2)."""
    conditions = [ChatSession.user_id == user_id, ChatMessage.role == "user"]
    if before_id is not None:
        conditions.append(ChatMessage.id < before_id)
    rows = session.execute(
        select(ChatMessage, Company.ticker)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .join(Company, Company.id == ChatSession.company_id)
        .where(*conditions)
        .order_by(ChatMessage.id.desc())
        .limit(limit)
    )
    return [(message, ticker) for message, ticker in rows.tuples()]


def add_message(
    session: Session,
    *,
    session_id: int,
    role: str,
    content: str,
    source_ids: list[str] | None = None,
    tool_trace: dict[str, Any] | None = None,
    cost_usd: Decimal | None = None,
    latency_ms: int | None = None,
) -> ChatMessage:
    row = ChatMessage(
        session_id=session_id,
        role=role,
        content=content,
        source_ids=source_ids,
        tool_trace=tool_trace,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )
    session.add(row)
    session.flush()
    return row


def set_feedback(
    session: Session, *, message_id: int, rating: str, reason: str | None
) -> ChatMessage | None:
    row = session.get(ChatMessage, message_id)
    if row is None:
        return None
    row.feedback = rating
    row.feedback_reason = reason
    return row


def purge_messages_before(session: Session, *, cutoff: datetime) -> int:
    """Delete chat messages older than `cutoff` (SEC-014: a real retention limit on logged user
    questions, not indefinite storage). The session shell is left in place — only message
    content is removed — so a later-generated eval candidate or support lookup by session id
    still resolves, just without the retained text."""
    result = session.execute(delete(ChatMessage).where(ChatMessage.created_at < cutoff))
    assert isinstance(result, CursorResult)  # noqa: S101  # DELETE always yields a CursorResult
    return result.rowcount
