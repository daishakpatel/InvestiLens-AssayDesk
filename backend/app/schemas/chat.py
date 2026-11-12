"""Chat request/response and citation schemas (spec §24.2, §13)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints

from app.schemas.research import EvidenceLabel


class Citation(BaseModel):
    """A rendered citation shown next to an answer (CIT-006)."""

    source_id: str
    number: int
    title: str | None = None
    section_path: list[str] = []
    page: int | None = None  # PDFs only
    tier: int | None = None


class ToolTraceEntry(BaseModel):
    tool: str
    latency_ms: int | None = None


# The question is sent to the model, so its size is a cost and abuse control (SEC-002, SEC-008).
# The assistant's composer enforces the same limit (frontend `MAX_QUESTION_LENGTH`).
MAX_QUESTION_LENGTH = 2000


class ChatRequest(BaseModel):
    company: str
    question: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_LENGTH),
    ]
    session_id: str | None = None


class ChatResponse(BaseModel):
    answer: str
    citations: list[Citation] = []
    evidence_label: EvidenceLabel | None = None
    abstained: bool = False
    refused: bool = False  # out-of-scope / advice refusal (distinct from abstention)
    tool_trace: list[ToolTraceEntry] = []
    suggested_questions: list[str] = []
    session_id: str | None = None
    message_id: str | None = None  # for POST /chat/messages/{id}/feedback


class ChatMessage(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    citations: list[Citation] = []


class ChatSession(BaseModel):
    id: str
    company: str
    messages: list[ChatMessage] = []


class FeedbackRequest(BaseModel):
    rating: Literal["up", "down"]
    # Free text from the reader: bounded at the trust boundary, stored as data only.
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None


class ReportFeedbackRequest(FeedbackRequest):
    """POST /feedback/report: on the whole report, or on one claim of it (spec §18.5)."""

    report_id: str
    claim_id: str | None = None
