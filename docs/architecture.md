# Architecture

Assay Desk is a **modular monolith**: one FastAPI deployable plus Celery workers, sharing one
PostgreSQL (with pgvector) and Redis ([ADR-0001](decisions/0001-modular-monolith.md)). Source:
spec §19–§20.

## Core engineering flow

Copied from spec §19.2. This separation is the central idea of the project and should not be reworded.

```text
                 ┌───────────────────────┐
                 │       USER            │
                 └───────────┬───────────┘
                             │
                             ▼
                 ┌───────────────────────┐
                 │      RESEARCH API     │
                 └───────────┬───────────┘
                             │
                             ▼
                 ┌───────────────────────┐
                 │     QUERY PLANNER     │
                 └───────────┬───────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        Financial DB    Vector Search    News DB
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                 ┌───────────────────────┐
                 │    EVIDENCE LAYER     │
                 └───────────┬───────────┘
                             │
                             ▼
                 ┌───────────────────────┐
                 │        LLM            │
                 │  Structured Output    │
                 └───────────┬───────────┘
                             │
                             ▼
                 ┌───────────────────────┐
                 │ CITATION VALIDATOR    │
                 └───────────┬───────────┘
                             │
                             ▼
                 ┌───────────────────────┐
                 │    RESEARCH REPORT    │
                 └───────────────────────┘
```

> **That separation is the core of the project.** The database and source documents provide
> the facts; deterministic Python provides the financial calculations; retrieval provides
> evidence; the LLM synthesizes the evidence; the citation layer verifies that the generated
> claims can be traced back to that evidence.

| Layer | Owns | Never does |
|-------|------|------------|
| Deterministic finance (`app/finance/`) | Every number: metrics, ratios, growth, quarters and TTM | Call an LLM |
| Retrieval (`app/rag/`) | Finding evidence; every item carries a backend-issued `source_id` | Generate text |
| LLM (`app/providers/` → `LLMClient`) | Synthesis and explanation of the evidence it is given | Produce numbers, invent sources |
| Citation validator (`app/citation/`) | Checking every claim against its cited source | Pass unverified claims through |

## Life of a research request

1. **Request.** A user asks for a report on `NVDA`. The API router validates input, checks
   auth/quota ([ADR-0005](decisions/0005-jwt-rotating-refresh-tokens.md),
   [ADR-0006](decisions/0006-anonymous-access-policy.md)), creates a `jobs` row, and returns a
   job ID. Progress streams to the client.
2. **Freshness check.** A Celery worker checks `data_freshness`. Stale sources trigger
   ingestion from SEC EDGAR, Tiingo, and Finnhub
   ([ADR-0007](decisions/0007-price-provider-tiingo.md),
   [ADR-0008](decisions/0008-news-provider-finnhub.md)). A failed provider degrades that panel
   visibly; it never blocks the whole report silently.
3. **Deterministic numbers.** The finance layer computes metrics from normalized XBRL facts and
   prices. Each result is registered as a `derived_metric` source with its formula and input
   source IDs.
4. **Query planning.** The planner splits the report into sections and decides, per section,
   what comes from structured data (numbers) and what needs retrieval (narrative evidence).
5. **Retrieval.** Hybrid vector + keyword search over filing chunks and news, fused and
   reranked ([ADR-0002](decisions/0002-pgvector.md),
   [ADR-0003](decisions/0003-custom-rag-orchestration.md)). Every candidate carries a
   `source_id` with a content anchor ([ADR-0004](decisions/0004-citation-anchors.md)).
6. **Evidence layer.** Selected evidence and computed metrics are assembled into a bounded
   context. Filing and news text is wrapped as untrusted data, never instructions.
7. **LLM synthesis.** The strong-tier model returns schema-validated structured output. Each
   claim references source IDs from the evidence set only
   ([ADR-0032](decisions/0032-llm-provider-gemini.md)).
8. **Citation validation.** The backend checks that every cited ID exists in the evidence set,
   that numbers in prose match computed metrics, and that the claim is supported by the source
   span. Failing claims are removed, not shown.
9. **Report.** The validated report is stored with its model, prompt version, data version,
   and retrieval set (reproducibility, NFR-010), then returned with clickable citations.

## Module boundaries

Business modules: Company, Filings, Financials, News, RAG, Research, Chat, Auth, Watchlist.
Modules talk through service interfaces; no module reads another module's tables except
through that module's repository.

**Layering** (spec §19.4): `api` (routers) → `services` → `repositories` → `models`.
`rag/`, `ingestion/`, `finance/`, `citation/`, `tasks/` are libraries consumed by services.
No business logic in routers; no SQL in services. External systems are reached only through
provider interfaces in `providers/` (`FilingsProvider`, `PriceProvider`, `NewsProvider`,
`LLMClient`, `EmbeddingClient`), each with a mock backed by frozen fixtures.

**Alerts** (Phase 7a, ADR-0039): `app/alerts/registry.py` holds one `AlertEventType` per kind
of alert (key, label, params model, deterministic detector, sample output). Dispatch
(`app/alerts/dispatch.py`, the 5-minute `send_alerts` job), the `/alerts` API and the frontend
rule form all read the registry, so a new event type is one `register(...)` call beside its
detector: no new query loop, dedupe or form. Dispatch asks each enabled alert's detector for events
after the alert's `watermark_at`, inserts one notification per `(alert, event_key)` with
`ON CONFLICT DO NOTHING`, then advances the watermark to the run's start. Each alert runs in its
own savepoint so one failing detector is logged and counted, never fatal to the run.

**Quarters and TTM** (Phase 7b, ADR-0040): `app/finance/quarterly.py` places each XBRL fact on the
company's own fiscal calendar (fiscal years from its 10-K spans; a period ending ~91/182/273 days
into a year ends Q1/Q2/Q3), never on calendar quarters, so NVIDIA's late-January year works the
same as a December one. Cash-flow items arrive year-to-date in 10-Qs and are de-cumulated
(Q2 = 6M − 3M, Q3 = 9M − 6M, Q4 = FY − 9M); income-statement items arrive as quarters, with
Q4 = FY − (Q1 + Q2 + Q3); balances are read at each quarter end. `quarterly_builder.py`, run by
`build_company_metrics`, stores the last 12 quarters and every complete four-quarter TTM (with
TTM margins and returns from TTM sums, never averaged quarterly ratios; each quarter's own margins
too, ADR-0073), each derived row citing its inputs,
and writes a `metric_gaps` row, with a reason, for every quarter or window it cannot compute.

**Earnings quality** (Phase 7d, ADR-0042): `app/finance/beneish.py` (8-variable M-score),
variant-aware Altman Z and nine-criteria Piotroski in `metrics.py`, and `quality_builder.py`.
The builder runs after valuation (Altman's original Z needs the year-end market cap) and stores
every measure as a derived FY row with its inputs. `GET /companies/{ticker}/earnings-quality`
reads them back with static explanations and peer percentiles. The claim verifier rejects fraud or
manipulation wording attached to a screening-score source (`citation/advice.py`).

**Red flags** (Phase 7e, ADR-0043): `app/redflags/` holds `taxonomy.py` (the rule registry,
`red_flag_taxonomy_v1`), `patterns.py` and `going_concern.py` (guarded phrase rules), `detect.py`
and `alerts.py`. `detect.scan_company` runs the rules over a company's 8-Ks, NT forms and
10-K/10-Q text, after parsing (the `parse_documents` task and the refresh chain), nightly
(`scan_red_flags`) and from `make redflags-backfill`. Flags are stored in `red_flags`, served by
`/companies/{t}/red-flags` and `/red-flags?watchlist=true`, cited as `redflag:` source IDs, and
alerted through the `red_flag` event type. No model is involved.

**Earnings briefs** (Phase 7j, ADR-0048): `app/earnings/` holds:
- `calendar.py` builds `earnings_events`: dates from Item 2.02 8-Ks, a company's own announced
  date, or an estimate (last year plus 52 weeks).
- `sections.py` assembles each section from stored 7b/7h/7i/7e/6b data.
- `watch.py` holds the deterministic watch candidates plus a checked rewording.
- `narrative.py` writes a ≤4-sentence summary through `citation.verify_claims`.
- `prompt.py` holds the one prompt, `earnings_narrative_v1`.
- `service.py` versions briefs by inputs hash and applies the watched/seed and daily-cap guards.
- `alerts.py` adds the `brief_ready` event type.

Briefs are made after filings and metrics land (`after_filings`, called at the end of
`update_financial_metrics` and the refresh chain), daily for previews of confirmed dates within 3
days (beat `earnings-previews` → `generate_due_previews`, 06:00 ET), by the
`generate_earnings_brief(company_id, kind)` task, and on a user's `POST
/companies/{t}/earnings/briefs`.

**Thesis tracker** (Phase 7k, ADR-0049): `app/thesis/` holds:
- `registry.py`: the trackable metrics, their units and series readers.
- `evaluator.py`: Decimal comparison, events only on data transitions.
- `links.py`: `refresh_company_theses`, which links new red flags and briefs and re-evaluates.
  It is called after `update_financial_metrics`, the refresh chain, `parse_documents`,
  `scan_red_flags`, a stored brief, and `scripts/build_metrics.py`.
- `alerts.py`: the `thesis_trigger` event type.
- `draft.py`: `thesis_draft_v1`, suggestions only.
- `service.py`: the API use cases.

No model is involved in any KPI state.

**SEC ingestion** (Phase 1a; exhibits and extra forms in Phase 7c, ADR-0041). `SecSource`
(`app/providers/sec/`, live EDGAR or frozen fixtures) offers `company_tickers`, `company_metadata`,
`list_filings(cik, since=)` (EDGAR's recent block, plus older `filings.files[]` pages when
`since` reaches past it), `companyfacts`, `fetch_document`, and `list_exhibits` /
`fetch_exhibit` (EX-99.x only, found in the filing's `-index-headers.html`). Every live call goes
through `HardenedHttpClient` (declared User-Agent, SEC fair-access rate limit, timeout, retry,
host allowlist).

```text
list_filings(since = longest backfill window)
   │  10-K 5y · 10-Q 12 quarters · 8-K / 8-K/A 2y · DEF 14A / DEFA14A 3y · NT 10-K / NT 10-Q 3y
   ▼
for each filing (SAVEPOINT) ── fetch_document ──► documents (filing | late_filing_notice) + filings
   │
   └─ 8-K with Item 2.02 / 7.01 / 8.01 (SAVEPOINT) ── list_exhibits ── fetch_exhibit
                                         ──► documents (earnings_release, tier 2, parent_accession)
   ▼
parse_company_documents: documents not at PARSER_VERSION ──► sections (10-K items, 10-Q P1-/P2-
   items, 8-K dotted items) ──► document_chunks;  late_filing_notice: metadata only
   ▼
embed_pending (EX-99 and DEF 14A only while EMBED_EXHIBITS is on)
```

## Data stores (table names only; columns are designed in Phase 0b)

- **Reference & filings:** `companies`, `company_identifiers`, `fiscal_calendars`,
  `documents`, `filings`, `document_chunks` (pgvector embeddings)
- **Financial data:** `financial_facts`, `financial_metrics`, `metric_gaps`, `price_history`,
  `corporate_actions`, `insider_transactions`, `institutional_holdings`
- **News:** `news`
- **Research & citations:** `research_reports`, `research_sources`, `claim_verifications`,
  `report_feedback`
- **Users:** `users`, `refresh_tokens`, `watchlists`, `watchlist_items`, `alerts`, `notifications`,
  `saved_reports`, `saved_questions`, `chat_sessions`, `chat_messages`
- **Operational:** `ingestion_runs`, `ingestion_dead_letters`, `jobs`, `llm_calls`,
  `retrieval_logs`, `data_quality_issues`, `eval_runs`, `eval_results`, `data_freshness`
- **Redis:** cache, rate limits, Celery broker
- **Object storage** (S3-compatible, MinIO locally): raw filing HTML/PDF

## External dependencies

| Domain | Provider | Decision |
|--------|----------|----------|
| Filings, XBRL, ticker↔CIK | SEC EDGAR (`data.sec.gov`, `sec.gov/Archives`) | Fixed (free, authoritative) |
| Prices | Tiingo | [ADR-0007](decisions/0007-price-provider-tiingo.md) |
| News | Finnhub | [ADR-0008](decisions/0008-news-provider-finnhub.md) |
| LLM | Google Gemini | [ADR-0032](decisions/0032-llm-provider-gemini.md) |
| Embeddings | Voyage AI `voyage-finance-2`, or Gemini `gemini-embedding-2` | [ADR-0010](decisions/0010-embeddings-voyage-finance.md), [ADR-0033](decisions/0033-embeddings-provider-gemini-option.md) |
| Transcripts | None in MVP | [ADR-0011](decisions/0011-transcripts-deferred.md) |
