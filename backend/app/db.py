"""Database engine and session factory.

The engine is created lazily so importing the models (e.g. for Alembic autogenerate or a
test that builds its own engine) never opens a connection as a side effect.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from functools import lru_cache
from weakref import WeakKeyDictionary

import anyio
import anyio.to_thread
from fastapi import HTTPException, status
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.utils.logging import get_logger, log_event

logger = get_logger(__name__)

# How many requests may hold a session at once: the pool's permanent size, so a request that is
# let through never has to wait for a connection (ADR-0029). The overflow is for sessions opened
# outside a request.
_REQUEST_SLOTS = 10
# A chat turn holds its session through embedding and model calls (seconds, not milliseconds), so
# chat has its own slots: under load it queues behind other chats, never in front of reads
# (ADR-0068). The pool holds both, so no admitted request waits for a connection.
_CHAT_SLOTS = 6
_POOL_OVERFLOW = 5
# How long a request waits for a slot before it is turned away with a 503, not queued forever.
_SLOT_WAIT_S = 10.0


@lru_cache
def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine."""
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        pool_size=_REQUEST_SLOTS + _CHAT_SLOTS,
        max_overflow=_POOL_OVERFLOW,
        future=True,
    )


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session: commits on success, rolls back on error, always closes."""
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Gates per event loop: asyncio primitives cannot be shared between loops, and tests run several.
_gates: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[bool, asyncio.Semaphore]] = (
    WeakKeyDictionary()
)


def _request_gate(*, chat: bool = False) -> asyncio.Semaphore:
    gates = _gates.setdefault(asyncio.get_running_loop(), {})
    if chat not in gates:
        gates[chat] = asyncio.Semaphore(_CHAT_SLOTS if chat else _REQUEST_SLOTS)
    return gates[chat]


async def get_chat_db() -> AsyncIterator[Session]:
    """`get_db` for a chat turn, on the chat gate (ADR-0068). Same scope rules as `get_db`."""
    async for session in _gated_session(chat=True):
        yield session


async def get_db() -> AsyncIterator[Session]:
    """FastAPI dependency: a session that always closes (overridable in tests).

    A request works in several steps, each on a borrowed worker thread (its dependencies, the
    handler, serialising the response), and keeps its connection across all of them. If it could
    also wait for a connection, requests holding a thread would wait for connections while
    requests holding connections wait for threads, and the API would freeze. So the wait happens
    here instead, on the event loop, before the request holds either (ADR-0029).

    Declare it as `Depends(get_db, scope="function")`, which ends the session before the
    response is sent: a client that reads slowly then does not keep a slot. Handlers return
    schema objects, never ORM rows.
    """
    async for session in _gated_session(chat=False):
        yield session


async def _gated_session(*, chat: bool) -> AsyncIterator[Session]:
    gate = _request_gate(chat=chat)
    try:
        await asyncio.wait_for(gate.acquire(), _SLOT_WAIT_S)
    except TimeoutError:
        log_event(logger, logging.WARNING, "db.slot_wait_timeout", wait_s=_SLOT_WAIT_S)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The service is busy; try again in a moment.",
            headers={"Retry-After": "1"},
        ) from None
    try:
        session = _session_factory()()  # no I/O yet: the connection is taken by the first query
        try:
            yield session
        finally:
            # Closing rolls back over the network, so it runs off the loop; shielded, because a
            # client hanging up must not leave the connection checked out.
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(session.close)
    finally:
        gate.release()
