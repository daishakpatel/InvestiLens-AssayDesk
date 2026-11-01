# RAG — Embeddings & Indexes (Phase 2b)

How `document_chunks` get embedded and how the two retrieval indexes are configured. Fusion,
reranking, query rewriting, and orchestration are Phase 2c and are **not** covered here. Sources:
spec §16.4, §22.4 (EMB-001…004), ADR-0010 (model), ADR-0013 (migration + tuning).

## Embedding model (ADR-0010)

| Property | Value |
|----------|-------|
| Provider / model | Voyage AI `voyage-finance-2` (ADR-0010), or Gemini `gemini-embedding-2` with `EMBEDDING_PROVIDER=gemini` (ADR-0033) |
| Dimension | 1024 (fixed) — `vector(1024)` column |
| `input_type` | `document` at index time, `query` at search time |
| Vectors | L2-normalized (cosine == the index metric) |
| Batch size | 128 inputs/request (Voyage cap); the Gemini client splits into requests of 100 |

Each chunk stores `embedding`, `embedding_model`, and `embedding_dim` (EMB-001) so a corpus can
hold mixed-model rows and re-embedding stays safe. `PROVIDER_MODE=mock` uses deterministic,
offline hash vectors of the same dimension; `live` uses Voyage (`VOYAGE_API_KEY` required, raises
if missing). `EMBEDDING_PROVIDER` overrides the mode for embeddings alone.

Vector search only matches chunks embedded by the active model, since distances across models
mean nothing. If the embedding provider fails or is over quota at query time, retrieval logs
`rag.vector_search.failed` and continues on keyword search alone.

Gemini's free tier embeds 100 texts a minute and 1,000 a day (measured 2026-10-12); questions
asked in the app draw on the same allowance. `scripts/embed_chunks.py`
commits each batch, waits and retries when a batch is refused, and can be stopped and restarted;
run it there with `EMBEDDING_BATCH_SIZE=80 EMBEDDING_RATE_LIMIT_PER_SEC=0.016`, which leaves room for
questions asked in the app meanwhile.

## Generation pipeline (EMB-002)

`app/embeddings/pipeline.py::embed_pending` reads chunks missing a vector for the active model,
embeds them in batches, and writes vectors back with their model + dim. **Every batch is logged to
`llm_calls`** (`purpose = "embedding"`) with token count, cost, and latency — the same cost table
used for chat completions, so embedding spend is tracked project-wide. Token counts come from
Voyage's reported usage when available, else the project `~4 chars/token` estimator (cost column
only; no financial output depends on it). Run: `cd backend && uv run python ../scripts/embed_chunks.py`.

## Re-embedding / model migration (EMB-003, ADR-0013)

Zero-downtime swap, in `app/embeddings/reembed.py`:

1. **Stage** — `stage_all` embeds every chunk under the new model into the `embedding_new` staging
   column. The live `embedding` column keeps serving reads the whole time. Resumable: only
   un-staged rows are embedded.
2. **Swap** — `swap` runs one atomic `UPDATE` promoting staged vectors to `embedding` and stamping
   the new `embedding_model` / `embedding_dim`, then clears staging. Only rows that were staged are
   touched, so a crash mid-backfill never loses data.

`migrate_model` runs both. A model with a **different dimension** requires a separate column-retype
migration (see ADR-0013); the swap mechanism assumes a dimension-preserving change (both Voyage
models are 1024-dim).

## Indexes

### Vector — HNSW cosine (EMB-004)

Index: `ix_document_chunks_embedding_hnsw` on `embedding`, `vector_cosine_ops`.

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `m` | 16 | pgvector default; graph degree is ample for a tens-of-thousands-chunk MVP corpus and keeps build fast. |
| `ef_construction` | 64 | pgvector default; good recall/build-time balance at this scale. |
| `ef_search` | 100 (per query) | Raised from the default 40 for better recall at negligible latency on this corpus; set `SET LOCAL hnsw.ef_search` per query so it can vary by query class. |

Values are revisited in the Phase 5b retrieval evals against measured recall/latency on the full
corpus. `ef_search` is `app.config.hnsw_ef_search` (default 100).

### Keyword — full-text GIN

Index: `ix_document_chunks_tsv` (GIN) on the generated `tsv` column
(`to_tsvector('english', text)`). Queries use `websearch_to_tsquery('english', …)` so English
stemming lets exact financial terms match differently-phrased queries ("gross margin" ↔ "gross
margins", "restricted cash", "deferred revenue", "customer concentration"). This is the safety net
for exact-term recall that pure semantic search can miss.

### Usage

`app/rag/search.py` exposes `vector_search`, `vector_search_by_vector`, and `keyword_search` —
each over **one** index, company-scoped (RAG-013). Combining them (RRF fusion, reranking) is the
Phase 2c pipeline below. An optional `SearchScope(section_keys, filed_since)` narrows both to some
sections (`chunk_metadata.section_key`) and to filings on or after a date; report sections use it
(ADR-0053). A scoped vector search sets `hnsw.iterative_scan = strict_order`, so it needs
pgvector >= 0.8.

Keyword search first requires every word of the query (`websearch_to_tsquery`). A full-sentence
question rarely passes that, so when the strict match is empty it widens to any of the words,
ranked by coverage. Without this, retrieval returns nothing whenever chunks have no embeddings.

---

# Retrieval pipeline (Phase 2c)

`app/rag/pipeline.py::run_retrieval(session, question=, ticker=)` returns a `RetrievalResult`:
ranked `Evidence` (each with a backend-issued `source_id`, typed anchor, scores, metadata), an
assembled context block, and a `sufficient` flag. It **generates no answer** — that is Phase
3a/3b/3c. Sources: §16, ADR-0003 (custom orchestration), ADR-0014 (reranker).

## Stages (§16.1)

1. **Intent classification** (`intent.py`, RAG-002) — rule-based, fixed label set, logged. Labels:
   `FINANCIAL_METRIC`, `RISK_ANALYSIS`, `FINANCIAL_EXPLANATION`, `DOCUMENT_SUMMARY`,
   `MANAGEMENT_COMMENTARY`, `COMPARISON`, `FILING_DIFF`, `OUT_OF_SCOPE_ADVICE`, `OUT_OF_SCOPE`.
   Advice/out-of-scope abstain before any retrieval.
2. **Query rewriting** (`rewrite.py`, RAG-003) — expand financial synonyms ("gross margin" ↔
   "gross profit percentage"), resolve relative time ("last three years" → fiscal years via
   `fiscal_calendars`), decompose explanation questions into sub-queries.
3. **Routing** (RAG-030) — `FINANCIAL_METRIC` is answered from `financial_metrics` (Phase 1c) via
   the tool layer, **never** through vector/keyword search.
4. **Retrieval** (`retrieval.py`, RAG-010…018) — vector + keyword search over the rewritten query
   and its sub-queries → **RRF fusion** (`fusion.py`, k=60) → **boilerplate dedup** (keep the
   latest of a `dedup_hash` cluster, annotate "unchanged since") → **rerank** (`rerank.py`) →
   **diversity cap** (≤ `rag_max_chunks_per_document`) + **time-aware** selection (recency boost
   for "latest"; one chunk per fiscal year for "over time") → **parent-child expansion** (attach
   sibling-section context; the citation stays the small child span).
5. **Context assembly** (`assembly.py`, RAG-020) — per-intent token budget; order structured
   metrics first, then Tier 1 evidence, then lower tiers; every evidence item wrapped in untrusted
   markers with its `source_id` + metadata.
6. **Sufficiency** (RAG-018) — if the top rerank score is below `rag_sufficiency_min_score`, the
   pipeline abstains (`sufficient=False`) rather than forcing a weak answer.
7. **Logging** — every call recorded to `retrieval_logs` (query, intent, top_k, scores, latency,
   chunk/source IDs) for the Phase 5b eval harness.

## Reranker (ADR-0014)

`LexicalReranker` (default) is deterministic and offline: stemmed query-term overlap blended with
the RRF prior, nudged by bounded tier/recency multipliers, normalized to `[0, 1]`.
`rag_rerank_enabled=False` falls back to `IdentityReranker` (pure fusion order) for latency
comparison. An LLM/cross-encoder reranker can slot in behind the `Reranker` ABC; Phase 5b evals
decide whether to switch. Because the lexical score floors near ~0.35 for zero-overlap hits (mock
embeddings return neighbors regardless of relevance), `rag_sufficiency_min_score` defaults to 0.4;
re-tune it per reranker.

## Tool layer (`tools.py`, RAG-031)

Typed, read-only, company-scoped tools, each returning a dict with `source_ids`:
`get_company_info`, `get_financial_metric`, `get_metric_series`, `calculate_growth` (delegates to
the deterministic finance layer — the LLM never computes), `get_recent_news`, `get_stock_history`.
`ToolRunner` enforces the per-question call budget (`rag_max_tool_calls`), detects loops (same
tool+args ≥ 3×), and logs every call with latency. P2 tools (`get_filing_diff`,
`get_insider_transactions`, `compare_companies`) are deferred.

## Prompt-injection defense (`injection.py`, RAG-040)

Retrieved text is data, never instructions. Phase 2a already stripped hidden HTML (DP-004). Here
every evidence span is wrapped in `<<<UNTRUSTED_SOURCE>>> … <<<END_UNTRUSTED_SOURCE>>>` markers
with a preamble that content between markers must never be followed; forged markers and control
chars are neutralized. The system/developer instruction is returned separately from the untrusted
block so the two are never concatenated into one instruction role. A red-team test confirms an
injected "ignore all previous instructions" chunk is wrapped as data and does not change pipeline
behavior.

## Config knobs

`rag_candidate_top_n` (40), `rag_rerank_top_k` (10), `rag_rrf_k` (60), `rag_rerank_enabled`,
`rag_sufficiency_min_score` (0.4), `rag_max_chunks_per_document` (3), `rag_context_token_budget`
(6000, scaled per intent), `rag_max_tool_calls` (6), `rag_tool_timeout_s` (10),
`llm_model_cheap`/`llm_model_strong` (model routing, RAG-050; the strong model is first used in
Phase 3).

## Deferred (first caller is Phase 3)

Semantic cache keyed on (company, normalized question embedding, data_version) (RAG-050) — the
hooks live in config/model routing, but the Redis-backed cache is built with its first real caller
in Phase 3c. A dedicated tool-call audit table (beyond structured logs) lands with Phase 5c
observability.
