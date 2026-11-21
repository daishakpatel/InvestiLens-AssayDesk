"""SSE event stream for a chat turn (§10.14, API-004).

The turn is computed up front (retrieval + verification are not token-streamed), then the verified
answer is emitted progressively as `token` events, followed by a `citations` event once the answer
completes and a terminal `done` event with the turn's metadata — so the UI shows text appearing and
citations resolving as the answer finishes, over a verified result (never raw model tokens).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator

from app.chat.service import ChatTurn

_WORD = re.compile(r"\S+\s*")


def _sse(event: str, data: object) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


async def sse_events(turn: ChatTurn) -> AsyncIterator[bytes]:
    """Yield SSE frames: token* → citations → done."""
    for token in _WORD.findall(turn.answer):
        yield _sse("token", {"text": token})
    yield _sse("citations", {"citations": [c.model_dump() for c in turn.citations]})
    yield _sse(
        "done",
        {
            "evidence_label": turn.evidence_label,
            "abstained": turn.abstained,
            "refused": turn.refused,
            "not_applicable": turn.not_applicable,
            "session_id": turn.session_id,
            "message_id": turn.message_id,
            "suggested_questions": turn.suggested_questions,
            "tool_trace": [t.model_dump() for t in turn.tool_trace],
        },
    )
