# Observability

How Assay Desk is instrumented, what's tracked, and how degradation is handled (spec §28,
OBS-001…005, ERR-001…004; decisions in [ADR-0021](decisions/0021-observability-stack.md) and
[ADR-0022](decisions/0022-redis-rate-limiting-and-ai-budget.md)).

## 1. Structured logging

`app/utils/logging.py` configures `structlog` to render every log line as JSON, with
`request_id` auto-attached via `contextvars` (bound once per request in `app/main.py`'s
`request_id_middleware`, API-007) so every line from a single request correlates without
threading an id through every function signature. A redaction processor masks any field whose
key looks secret-shaped (`password|secret|token|authorization|api_key`) before rendering
(OBS-004) — belt-and-suspenders, since no call site currently logs such a field.

```json
{"event": "chat.turn", "level": "info", "timestamp": "...", "request_id": "...", "intent": "financial_metric", "latency_ms": 184}
```

The call-site API (`get_logger`, `log_event`, `bind_request_id`) is unchanged from earlier
phases — adopting structlog touched zero call sites, only the implementation underneath them.

## 2. Tracing (OBS-001)

OpenTelemetry spans cover the full life of a question:

```text
chat.answer_question (company)
  └─ rag.run_retrieval (company, intent)
       └─ chat.synthesize (model)            [only for qualitative questions — metric
                                               questions skip the LLM entirely, RAG-030]
  └─ citation.verify (claims)
```

(Report generation has the equivalent chain: `research.generate_section` per section, tagged
with `prompt_version` and `section`, inside `generate_report`.)

The default exporter prints spans to stdout (`ConsoleSpanExporter`) — zero infra required for
local dev or CI. Setting `otel_exporter_otlp_endpoint` switches to a real collector
(Jaeger/Tempo/Honeycomb/...) with no code change. `tests/unit/test_tracing.py` proves the
instrumentation for real, using `InMemorySpanExporter` to assert actual span names/attributes —
not just that the code runs without crashing.

## 3. Metrics (OBS-002)

`GET /metrics` (root-level, not under `/api/v1` — a scrape endpoint, not a business API;
`include_in_schema=False` so it never pollutes the OpenAPI contract) exposes Prometheus metrics
from `app/observability/metrics.py`:

| Metric | Kind | Updated |
|---|---|---|
| `api_request_duration_seconds{method,route,status}` | histogram | every request, via middleware — `histogram_quantile(0.5/0.95/0.99, ...)` gives p50/p95/p99 |
| `llm_call_duration_seconds{purpose,model}` / `llm_cost_usd_total{purpose,model}` | histogram / counter | every `llm_calls.record_call` (the one choke point every LLM call already goes through — see §5) |
| `retrieval_top_score{intent}` | histogram | every `rag.pipeline.run_retrieval` call — the retrieval score distribution |
| `citation_verifications_total{status}` | counter | every `claim_verifications.record_verifications` call — rejection rate = `rejected / total` |
| `ingestion_lag_minutes{source}` | gauge | computed fresh from `data_freshness` on each scrape |
| `job_failure_rate` / `ingestion_failure_rate` | gauge | computed fresh from the last 20 `jobs`/`ingestion_runs` rows on each scrape |
| `job_queue_depth` | gauge | `COUNT(*) FROM jobs WHERE status='queued'` on each scrape |
| `cache_hit_ratio` | gauge | **always `NaN`** — see §6 |

`observability/grafana/dashboards/assay-desk.json` is a ready-to-provision dashboard over these
exact metric names; `observability/prometheus.yml` + an optional `prometheus`/`grafana` pair in
`docker-compose.yml` (`--profile observability`) wire it up for a real deployment. None of this
needs to run for local dev — the metrics are correct and scrapeable from `/metrics` directly
(`curl localhost:8000/metrics`) with no Prometheus running at all.

## 4. Alerting (OBS-003)

`app/observability/alerts.py`'s `evaluate_alerts(session)` is a pure function over the same DB
state the metrics above read — deliberately independent of a running Prometheus/Alertmanager, so
the DoD's "alerts fire correctly in a simulated failure" is testable in CI with zero infra:
`tests/integration/test_alerts.py` seeds the exact DB state that should cross each threshold
(e.g. a `data_freshness` row 65 minutes stale) and asserts the alert fires, and that healthy
state doesn't fire one.

`observability/alerts.rules.yml` mirrors the same five thresholds as real Prometheus alerting
rules for a deployment that *is* running Prometheus. The Python module is the source of truth
(and what CI actually exercises); keep the YAML in sync by hand when a threshold in
`app/config.py` changes — see the ADR-0021 docstring in `alerts.py` for why these aren't
generated from one source automatically (no infra to generate against in this environment).

| Alert | Threshold | Severity |
|---|---|---|
| `ingestion_lag` | > 60 min since last successful ingest, any source | critical |
| `job_failure_spike` | > 20% of the last 20 jobs failed | critical |
| `ingestion_failure_spike` | > 20% of the last 20 ingestion runs failed | critical |
| `llm_spend_over_budget` | today's total LLM spend > $25 (global circuit-breaker signal, distinct from the per-user budget, ADR-0022) | warning |
| `sec_429_repeated` | > 5 retryable 429s in the recent window (caller-supplied count) | warning |
| `error_rate_slo_burn` (Prometheus-only; no DB state to simulate in Python) | > 5% of requests 5xx over 5m | critical |

## 5. `llm_calls` completeness (OBS-005)

Every LLM call in the system logs to `llm_calls` (tokens, cost, latency, prompt version,
status) through exactly three choke points: `app/chat/service.py` (purpose=`chat`),
`app/embeddings/pipeline.py` (purpose=`embedding`), and `app/research/generator.py`
(purpose=`report_section`, logs **both** the initial attempt and the one repair retry).
Query rewriting, reranking, and news categorization are rule-based/deterministic (ADR-0003,
ADR-0014) — they never call an LLM, so there is nothing to log for them.

**Audit finding, fixed this phase:** `app/research/generator.py` called `llm.complete_json`
directly and never logged to `llm_calls` at all — report-generation cost/latency was invisible
to the `llm_calls` table (and therefore to the cost dashboard) even though the report row itself
tracked aggregate `latency_ms`. Fixed by threading `session`/`user_id` through
`generate_section`/`_call` (both optional, so offline unit tests that don't need persistence are
unaffected) into a new `_logged_complete_json` wrapper, mirroring the existing chat/embeddings
pattern. Regression test: `tests/integration/test_report_generation.py::test_report_generation_logs_every_llm_call`.

**Second finding, fixed this phase:** chat called `citation.pipeline.verify_text` but never
`persist`/`record_verifications`, so chat-turn claim verifications never reached
`claim_verifications` — the citation-rejection-rate metric and dashboard only ever saw
report-generation claims, never chat (which is almost certainly the dominant source of
verifications once live). Fixed in `app/chat/service.py::_finish`. Regression test:
`tests/integration/test_chat.py::test_qualitative_question_logs_claim_verifications`.

## 6. Cache hit rate — an honest placeholder

`cache_hit_ratio` always reports `NaN`. **No cache layer exists yet** — the RAG-050 semantic
cache (keyed on company + normalized question embedding + data version) was deferred past
Phase 2c/3c/5b and is still not built; there is no read-through response cache either. Building
one was explicitly out of scope for this phase ("instruments and hardens what already exists,"
not new features). The metric name, Grafana panel, and this note exist so the dashboard is ready
the day a cache lands, rather than silently fabricating a number with nothing behind it.

## 7. Data freshness (§28.3, FRESH-001)

`data_freshness` has one row per `(company, source)` with `fresh`/`stale`/`failed` states,
surfaced via `GET /meta/data-freshness/{ticker}` and every data endpoint's `freshness` envelope
(API-008). The frontend's `FreshnessBadge` (`frontend/src/components/Panel.tsx`) renders it
per-panel: a neutral dot + "Updated ..." when fresh, a warning triangle when stale, and
`"Refresh failed · cached <date>"` when failed — the cached data stays visible and clearly
labeled rather than disappearing (§28.2's "SEC unavailable... showing previously cached data,"
implemented as a per-source badge rather than a page-level banner, since different panels on the
same dashboard can be independently stale).

**Audit finding, fixed this phase:** SEC ingestion (`app/ingestion/sec/pipeline.py`) never wrote
`data_freshness` at all — only prices and news did. A company that ingested successfully would
show `source=sec` as permanently `stale` (the default for a missing row) forever, and a SEC
provider outage during `list_filings` (not wrapped in any try/except) would crash `ingest_company`
entirely rather than degrade — the run would stay `running` forever and nothing would ever tell
the API or the user that SEC data was unavailable. Fixed: `list_filings` is now called inside a
try/except that dead-letters, records `data_freshness(status="failed")`, and finishes the run as
`failed` without raising; a successful run now records `status="fresh"`. Regression tests:
`tests/integration/test_sec_ingestion.py::test_ingest_populates_all_tables` (freshness fresh on
success) and `::test_provider_outage_degrades_without_crashing` (simulated EDGAR outage).

## 8. Degradation matrix (ERR-001, §28.2)

| Scenario | What happens | User-facing result | Verified by |
|---|---|---|---|
| **SEC unavailable** (EDGAR down/429s exhausted) | `ingest_company` dead-letters, marks `data_freshness(sec)` failed, finishes the run failed — never crashes, never leaves stale data silently looking fresh | Existing financial data/filings stay visible; their panel shows "Refresh failed · cached \<date\>" instead of a generic error | `test_sec_ingestion.py::test_provider_outage_degrades_without_crashing` |
| **Price/news unavailable** | Same pattern, already existed pre-Phase-5c (`app/ingestion/prices/pipeline.py`, `app/ingestion/news/pipeline.py`) — dead-letter + `data_freshness(status="failed")` + run finished failed | Same per-panel "Refresh failed · cached" badge | pre-existing ingestion tests (`test_price_ingestion.py`, `test_news_ingestion.py`) |
| **AI failure — report generation** | A section's LLM call/parse fails even after the one repair retry → `_run_section` catches it and marks that section `insufficient_evidence_sections`; the report still assembles and persists with every *other* section intact (never a whole-report failure for one bad section). When every section's model call fails, `generate_report` raises `ReportGenerationError` before saving, so the job fails and the previous report stays the latest (ADR-0056) | `ResearchTab` shows "Unable to generate the research report. Your financial data remains available —" with a link to the data that *is* there (`frontend/src/pages/tabs/ResearchTab.tsx`) | `test_report_generation.py` (insufficient-section handling) |
| **AI failure — chat** | No evidence / insufficient retrieval / failed verification → explicit abstention (`"I couldn't find evidence..."`), never a guess; an unexpected exception anywhere in the request is now caught by the catch-all handler below | Chat shows the abstention as an intentional `Notice`, not an error state (`ChatTab`) | `test_chat.py::test_abstention_on_no_evidence` |
| **Embedding provider unavailable** (down, or over quota at query time) | `retrieve_evidence` catches the failure, logs `rag.vector_search.failed`, and ranks on keyword search alone, so the answer or report still proceeds on weaker evidence. News ingestion saves the articles without near-duplicate grouping (identical headlines still group), logs `news.cluster.skipped` and records `clustering: "skipped"` on the run | `test_embedding_outage_falls_back_to_keyword_search` |
| **Any unhandled exception** (DB blip, unexpected bug, anything not one of the above) | **Audit finding, fixed this phase:** there was no catch-all exception handler — an unhandled exception bypassed RFC 7807 entirely and risked leaking a stack trace depending on debug settings (ERR-003 violation). Fixed: a global `Exception` handler (`app/api/errors.py`) logs the real exception + traceback server-side only (`exc_info=True`, structured JSON, correlated by `request_id`) and returns a generic RFC 7807 `500` with no internal detail | `{"title": "Internal Server Error", "detail": "An unexpected error occurred. Your data is unaffected; please try again.", "request_id": "..."}` | `tests/unit/test_error_handling.py` — raises an exception carrying a fake secret and a distinctive type name, asserts neither (nor "Traceback") ever appears in the response |

No stack trace or internal exception detail is ever returned to a client: `tests/unit/test_error_handling.py::test_unhandled_exception_returns_generic_rfc7807_500` raises a `ValueError` carrying a fake secret string and asserts the secret, the exception type name, and "Traceback" never appear anywhere in the response — only the server-side structured log (via `log_event(..., exc_info=True)`) ever sees them.
