# ADR-0072: Chat query embeddings are cached in Redis

- **Status:** Accepted
- **Date:** 2026-11-23
- **Spec refs:** §8, NFR-002, RAG-010, CACHE-001, CACHE-005, ADR-0024, ADR-0068

## Problem

Chat retrieval missed its 400 ms target (NFR-002) at 662 ms p95 for a single user; about 320 ms
was one round trip to Gemini for the question's embeddings (ADR-0068 had already batched the
four rewrites into one call). A model's vector for a given text never changes, and suggested
questions and re-asks repeat the same texts.

## Decision

`vector_search_many` reads each query's vector from the cache family `query_embedding`, keyed by
the embedding model and the SHA-256 of the text, with a 30-day TTL, and embeds only the misses,
still in one call. A new `cache.get_or_load_many` does one MGET and one pipelined write; any Redis
error reads as all misses, a failed write is ignored, and the loader never runs twice (CACHE-005).
It follows `CACHE_ENABLED` like the rest of the cache, so tests and local dev without it are
unchanged.

## Consequences

A repeated question's retrieval took 219-263 ms against 895 ms on its first ask (dev DB, one
user): under the target. A new question still pays the round trip, so p95 under varied traffic
stays above 400 ms; a closer or local embedding model is the remaining lever. Changing the
embedding model changes the key, so stale vectors are never mixed with new ones.
