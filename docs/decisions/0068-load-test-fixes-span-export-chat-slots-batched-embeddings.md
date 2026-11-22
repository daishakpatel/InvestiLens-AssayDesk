# ADR-0068: Load-test fixes: batched span export, chat DB slots, one query-embedding call

- **Status:** Accepted
- **Date:** 2026-11-22
- **Spec refs:** §8, NFR-001, NFR-002, NFR-004, OBS-001, ADR-0021, ADR-0029

## Problem

The first load test (50 users, half chat, one uvicorn process; `docs/testing.md`) missed every
read target: company p95 2.5 s, search 1.5 s, and 2% of all requests were 503s after a 10 s wait.
Three causes. The tracer exported each span inline (`SimpleSpanProcessor` + console exporter),
serialising JSON inside every request; read p95 was 410–450 ms even with no chat traffic. Chat
turns held one of the 10 shared DB slots (ADR-0029) for their whole turn, including embedding
calls, so reads queued behind them. And a chat question's four query rewrites were embedded in
four sequential round trips.

## Decision

The app's default exporter runs behind a `BatchSpanProcessor`; an exporter a test injects stays
synchronous, and the test suite installs a discard exporter so nothing prints after pytest closes
its output. `POST /chat` and `/chat/stream` take their session from `get_chat_db`, which waits on
its own gate of `_CHAT_SLOTS = 6`; the pool is `_REQUEST_SLOTS + _CHAT_SLOTS`, so ADR-0029's rule
(an admitted request never waits for a connection) still holds. `retrieve_evidence` embeds all
its queries in one call (`vector_search_many`), falling back to keyword search as before.

## Consequences

Under the same load, read p95 is 120–190 ms and chat p95 2.6 s (mock model), with no failures.
Under heavy chat load, chat queues behind other chats instead of delaying reads; ten chat slots
were tried and cost reads 500–600 ms p95 through CPU contention, so more chat capacity means more
API instances. Retrieval still misses its 400 ms target (662 ms single user), mostly one remote
embedding round trip; a local or cached query embedding would be the next step.
