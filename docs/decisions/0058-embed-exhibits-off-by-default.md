# ADR-0058: Exhibits and proxies are not embedded by default

- **Status:** Accepted
- **Date:** 2026-11-07
- **Spec refs:** FR-EX-013, NFR-008

## Problem

Task 28 set `EMBED_EXHIBITS` to default true, which was harmless with mock embeddings. With real
embeddings it is the costliest default in the app: a DEF 14A is about 1-2k chunks, and Celery beat
fires overdue jobs as soon as it starts, so the first beat run on a fresh setup embeds the whole
backlog of proxies and EX-99 press releases (about 14,000 chunks in the dev database). On
2026-11-03 that helped use up the prepaid Gemini credit. The dev `.env` already sets it false.

## Decision

`Settings.embed_exhibits` defaults to `False`. Proxies and press releases stay keyword-searchable;
a deployment that wants them in vector search sets `EMBED_EXHIBITS=true`.

## Consequences

A fresh setup spends nothing on documents that rarely answer report or chat questions, and the
paid path is opt-in. Semantic search misses proxy and press-release passages unless the flag is on;
keyword search and the governance/earnings features that read those documents directly are
unaffected. Revisit if retrieval evals show those documents answering questions keyword search
misses.
