"""Chat Q&A endpoints (spec §24.2, §13, §10.14). Streaming via SSE (API-004).

Thin HTTP layer over `app.chat.service`: resolve the user (ADR-0006), run one verified turn, and
map it to the response / SSE stream. Chat requires an account; the user is taken from the
`get_current_user_id` dependency (X-User-Id until Phase 4b wires JWT).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user_id
from app.api.pagination import NEXT_CURSOR_HEADER, clamp_limit, decode_cursor, encode_cursor
from app.chat.service import ChatTurn, answer_question
from app.chat.streaming import sse_events
from app.config import get_settings
from app.db import get_db
from app.repositories import chat as chat_repo
from app.schemas.chat import (
    AskedQuestion,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatSession,
    Citation,
    FeedbackRequest,
)

router = APIRouter(prefix="/chat", tags=["Chat"])

_DB = Depends(get_db, scope="function")
_USER = Depends(get_current_user_id)


def _to_response(turn: ChatTurn) -> ChatResponse:
    return ChatResponse(
        answer=turn.answer,
        citations=turn.citations,
        evidence_label=turn.evidence_label,
        abstained=turn.abstained,
        refused=turn.refused,
        tool_trace=turn.tool_trace,
        suggested_questions=turn.suggested_questions,
        session_id=str(turn.session_id) if turn.session_id is not None else None,
        message_id=str(turn.message_id) if turn.message_id is not None else None,
    )


def _session_id(body: ChatRequest) -> int | None:
    if not body.session_id:
        return None
    try:
        return int(body.session_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid session_id") from exc


@router.post("", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    db: Session = _DB,
    user_id: int = _USER,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ChatResponse:
    turn = answer_question(
        db,
        company=body.company,
        question=body.question,
        user_id=user_id,
        session_id=_session_id(body),
    )
    db.commit()
    return _to_response(turn)


@router.post("/stream")
def chat_stream(body: ChatRequest, db: Session = _DB, user_id: int = _USER) -> StreamingResponse:
    """SSE token stream over a verified answer (API-004)."""
    turn = answer_question(
        db,
        company=body.company,
        question=body.question,
        user_id=user_id,
        session_id=_session_id(body),
    )
    db.commit()
    return StreamingResponse(sse_events(turn), media_type="text/event-stream")


@router.get("/questions", response_model=list[AskedQuestion])
def list_my_questions(
    response: Response,
    limit: int | None = None,
    cursor: str | None = None,
    db: Session = _DB,
    user_id: int = _USER,
) -> list[AskedQuestion]:
    """The caller's past questions, newest first (§26.2); kept only as long as SEC-014 allows."""
    if get_settings().demo_mode:
        return []  # the demo's one shared account would show visitors each other's questions
    page_size = clamp_limit(limit)
    rows = chat_repo.questions_for_user(
        db, user_id=user_id, limit=page_size + 1, before_id=decode_cursor(cursor)
    )
    if len(rows) > page_size:
        rows = rows[:page_size]
        response.headers[NEXT_CURSOR_HEADER] = encode_cursor(rows[-1][0].id)
    return [
        AskedQuestion(
            message_id=str(message.id),
            ticker=ticker,
            question=message.content,
            asked_at=message.created_at.isoformat(),
        )
        for message, ticker in rows
    ]


@router.get("/sessions/{session_id}", response_model=ChatSession)
def get_session(session_id: int, db: Session = _DB, user_id: int = _USER) -> ChatSession:
    row = chat_repo.get_session(db, session_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    messages = chat_repo.recent_messages(db, session_id=session_id, limit=100)
    return ChatSession(
        id=str(session_id),
        company=str(row.company_id),
        messages=[
            ChatMessage(
                id=str(m.id),
                role="user" if m.role == "user" else "assistant",
                content=m.content,
                citations=[
                    Citation(source_id=s, number=i + 1) for i, s in enumerate(m.source_ids or [])
                ],
            )
            for m in messages
        ],
    )


@router.post("/messages/{message_id}/feedback", status_code=204)
def message_feedback(
    message_id: int, body: FeedbackRequest, db: Session = _DB, user_id: int = _USER
) -> None:
    updated = chat_repo.set_feedback(
        db, message_id=message_id, rating=body.rating, reason=body.reason
    )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")
    db.commit()
