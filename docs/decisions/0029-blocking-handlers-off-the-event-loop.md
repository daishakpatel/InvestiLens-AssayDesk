# ADR-0029: Blocking request handlers run off the event loop, behind a connection gate

- **Status:** Accepted
- **Date:** 2026-10-11
- **Spec refs:** §24 (API), NFR-001, SEC-008

## Problem

Database access is synchronous (SQLAlchemy `Session` over psycopg), but 51 route handlers were
declared `async def`, so their queries, and the LLM call behind `/chat`, ran on the event loop.
A request keeps its connection until its `get_db` teardown, which the loop has to schedule. Once
the pool's 15 connections were out, the next handler blocked the loop waiting for one, the
teardowns that would have freed one could not run, and every endpoint, `/health` included, froze
until the 30-second pool timeout, then did it again. Forty overlapping requests reproduced it:
31 timed out after 45 seconds. A lock wait inside a handler froze the API the same way.

Moving the handlers to worker threads was necessary but not enough. A request then runs as
several steps on borrowed threads (its dependencies, the handler, serialising the response)
and holds its connection across all of them. With more requests than threads, the threads fill
with handlers waiting for a connection while the connections belong to requests waiting for a
thread: 120 overlapping requests still stalled for 30 seconds.

## Decision

1. A route handler that takes a database session, or otherwise blocks, is a plain `def`, which
   FastAPI runs in a worker thread. `async def` is kept for code that only awaits (`/health`,
   the rate limiter, the middleware).
2. (Amended by ADR-0068: chat turns use `get_chat_db` and their own `_CHAT_SLOTS` (6); the pool
   holds both.) `get_db` admits at most `_REQUEST_SLOTS` (10) requests at a time, waiting on an asyncio
   semaphore before the request holds a thread or a connection. The pool is sized to match
   (`pool_size=10`, `max_overflow=5` for sessions opened outside a request), so an admitted
   request never waits for a connection, and a thread is only ever busy with real work.
3. The dependency is declared `Depends(get_db, scope="function")`, which closes the session
   before the response is sent, so a client that reads slowly does not keep a slot. Handlers
   therefore return schema objects, never ORM rows.

The session layer stays synchronous; `AsyncSession` would touch every repository for no gain at
this scale. `tests/unit/test_api_contract.py::test_database_handlers_cannot_stall_the_api` fails
the build if a handler that takes a session is a coroutine or declares it without the scope, and
`tests/integration/test_request_concurrency.py` runs eight overlapping requests against one
connection and two threads.

## Consequences

Overlapping requests queue instead of deadlocking: 400 at once complete in about 1.4 seconds on
a laptop, where 40 used to freeze the API. A slow query or LLM call no longer stalls unrelated
requests. Database concurrency per API process is capped at 10; beyond that, requests wait in
memory for up to 10 seconds (`_SLOT_WAIT_S`, added 2026-10-29) and are then turned away with a
logged 503 and `Retry-After: 1`, so sustained overload fails fast instead of reaching the proxy's
timeout. Revisit the slot count and the wait when load testing gives numbers; revisit the whole arrangement if a second session per request ever
becomes necessary, because the gate assumes one.
