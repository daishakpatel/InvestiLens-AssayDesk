# Testing Strategy

How Assay Desk is tested, what runs where, and how to run each suite. Source: spec §30 (testing
strategy) and §8 (NFR latency targets); decisions in [ADR-0019](decisions/0019-testing-strategy-and-ci-gates.md).

## Tiers

| Tier | Suites | Where | Gate? |
|------|--------|-------|-------|
| **1 — per-PR** | ruff + mypy --strict, backend pytest (unit + integration + migration) under coverage, frontend eslint + tsc + Vitest, frontend build | `make check` locally; `ci.yml` on every push/PR | **Yes** (merge gate) |
| **2 — on-demand** | Playwright E2E happy path + axe accessibility | `e2e/`; `e2e.yml` (`workflow_dispatch`) | No |
| **3 — on-demand** | Locust load test vs NFR latency targets | `load/`; run against a deployed stack | No |

Tier 1 is fully offline and reproducible (`PROVIDER_MODE=mock`, no network, DR-004). Tiers 2–3
need a running, seeded stack (and, for a full E2E, an LLM key or the Fake-LLM seed), so they are
not on the per-PR path — see ADR-0019 for why.

## Backend suites (`backend/tests/`)

Every test references at least one requirement ID (conventions.md). Highlights:

- **Unit — financial formulas** (`unit/test_metrics.py`): 100% of `app/finance/metrics.py`; golden
  exact-match (NVDA/AAPL/JPM), reason codes (DR-041), sector applicability (DR-042), lineage
  (DR-040), plus Hypothesis invariants (CAGR/YoY/margin/division).
- **Unit — property invariants** (`unit/test_properties.py`): RRF fusion is deterministic,
  commutative, sorted, and preserves ids (RAG-011); no chunk ever crosses a section boundary
  (CH-001).
- **Unit — citation** (`unit/test_citation.py`, `unit/test_citation_coverage.py`): the five-layer
  verifier (CIT-005) — unknown-source rejection, numeric match/mismatch + tolerance, causal
  over-reach, coverage; first-appearance numbering (CIT-006); tiers (§15); every reference-label
  and resolver branch (→ 100% of `app/citation`).
- **Unit — security corpus** (`unit/test_security_corpus.py`): a corpus of prompt-injection and
  XSS payloads run through the untrusted-content defenses — hidden-markup stripping (DP-004),
  delimiter/control-char neutralization and data-wrapping (RAG-040), and proof that script/XSS
  markup never survives into chunk text.
- **Unit — resilience** (`unit/test_resilience.py`): provider 429/500/timeout → retry+backoff →
  single typed `HttpClientError` (DR-002); transient failure then recovery; malformed LLM JSON →
  one repair → section marked insufficient, never a crash (§17.2).
- **Integration** (`integration/`): API → DB → retrieval → report/chat against real
  Postgres 16 + pgvector; auth + IDOR (AUTH-004/SEC-007); partial-ingestion isolation and
  dead-lettering (`test_sec_ingestion.py::test_partial_failure_is_dead_lettered`); per-section
  report isolation (`test_report_generation.py::test_section_failure_isolated`); migrations
  up/down on empty and populated DBs (`test_migrations.py`). These skip when no Postgres is
  reachable (offline), and run in CI via the `pgvector/pgvector:pg16` service.

LLM/embedding calls use deterministic fakes/recorded fixtures (Phase 0d) — never live models in
the default run.

### Observability & security (Phase 5c, §28/§29)

- **Unit — tracing** (`unit/test_tracing.py`): OTel spans actually carry the attributes OBS-001
  requires, asserted via `InMemorySpanExporter` (no collector running).
- **Unit — error handling** (`unit/test_error_handling.py`): the catch-all exception handler
  never leaks a stack trace or exception detail into an RFC 7807 response (ERR-003).
- **Unit — rate limiting** (`unit/test_rate_limit.py`): the in-process and Redis-backed (via
  `fakeredis`) limiters, plus the middleware's two tiers (general vs AI) end to end.
- **Unit — billing** (`unit/test_billing_cost.py`): LLM cost estimation is `Decimal`, scales with
  model tier, never negative.
- **Integration — alerts** (`integration/test_alerts.py`): each of the five DB-driven alert rules
  fires on a seeded "simulated failure" and stays silent on healthy state (the DoD's explicit
  "alerts fire correctly in a simulated failure" requirement, testable with no Prometheus
  running).
- **Integration — budget/IDOR/retention/injection** (`integration/test_chat.py`,
  `integration/test_report_generation.py`, `integration/test_auth.py`,
  `integration/test_api_endpoints.py`, `integration/test_injection_production_shaped.py`): AI
  budget enforcement blocks the LLM-cost path only (metric answers stay free); `/alerts` is now
  IDOR-tested like watchlists/chat sessions; `hard_delete_user` cascades to owned data but keeps
  public reports (`user_id` nulled); the Phase 2c/3c red-team corpus re-run against a **real**
  NVDA 10-K fixture through the real parse→chunk→retrieve→chat pipeline, not just synthetic
  strings.
- **Integration — Redis** (`integration/test_rate_limit_redis.py`): the Redis-backed limiter
  against a real Redis, skipping if unreachable (mirrors the Postgres skip-offline pattern,
  ADR-0019); CI's `redis:7-alpine` service makes it run there.
- **Integration — SEC degradation** (`integration/test_sec_ingestion.py`): a simulated EDGAR
  outage degrades (dead-letter + `data_freshness` failed + run failed) instead of crashing —
  closes a gap where SEC ingestion never wrote `data_freshness` at all.

### Background jobs, cache & alerts (Phase 5d, §25-26, ADR-0024/0025)

- **Integration — jobs** (`integration/test_tasks.py`): the `jobs`-row lifecycle (running → done
  with progress, JOB-002), terminal failure → `failed` dead-letter row vs. a retriable attempt
  that stays `running` (JOB-001), and real Celery tasks run in eager mode on both the `batch` and
  `ingestion` queues, idempotent under a repeat run (JOB-003/005).
- **Unit — cache** (`unit/test_cache.py`): versioned/namespaced keys, miss→hit, company
  invalidation, and the headline guarantee — **Redis down falls back to the DB loader, never
  failing the request** (CACHE-005), via a raising fake client.
- **Integration — alert dispatch** (`integration/test_alert_dispatch.py`): the DoD scenario — a
  simulated filing ingestion triggers a notification; plus price-move dispatch, email-channel
  delivery through a capturing backend, idempotency (watermark advance), and the
  `GET /notifications` read path.
- **Verified manually (infra-bound, not in CI):** a real Celery worker boots against Redis and
  registers all 14 tasks; a broker round-trip (`purge_chat_history.delay()`) executes and records
  a `done` job row (local macOS needs `--pool solo`; Linux uses prefork). A backup→restore
  round-trip through `pg_dump`/`pg_restore` reproduced all row counts exactly (companies/filings/
  facts) into a fresh database — the DoD's "backup restore actually tested once".

### Coverage gate (NFR-013)

`make coverage` runs the suite once under coverage and enforces two hard thresholds:

```
make coverage        # pytest --cov=app, then: fail-under 80 overall, fail-under 100 on finance+citation
```

CI runs the same as the backend `pytest` step. Local `make check` stays plain `pytest` so the dev
loop is fast.

## Frontend suites (`frontend/src/**/*.test.ts`, Vitest)

Pure-module tests, no DOM/React renderer (minimal toolchain): response validation accepts valid /
rejects malformed payloads (UI-001, `schemas.test.ts`); citation numbering first-appearance order
(CIT-006, `reportCitations.test.ts`); display formatting + unit conversion (`format.test.ts`); and
a source guard that fails the build if `dangerouslySetInnerHTML` is ever introduced (XSS,
`xssGuard.test.ts`). Run with `npm run test`.

## Evaluation harness (`backend/app/eval/`, spec §18)

Measures retrieval, answer, citation, hallucination, and abstention quality against the golden set
(`backend/tests/eval/golden_v0.jsonl`, 100+ questions built by `scripts/build_golden.py`). It
MEASURES the Phase 2c/3a pipeline — it never re-implements retrieval or verification.

- **Metrics** (`metrics.py`, unit-tested): Precision@K, Recall@K, MRR, NDCG (document-level vs
  `expected_sources`); numeric answer accuracy (Decimal, within tolerance); citation accuracy +
  hallucination (from the Phase 3a verifier's exposed labels); abstention correctness.
- **Judge** (`judge.py`): `LLMJudge` (versioned prompt `judge_v1`) for qualitative faithfulness,
  `FakeJudge` offline; `calibrate()` reports agreement vs human labels (target ≥ 0.85, §18.4).
- **Runner** (`runner.py`): drives each question through `chat.answer_question`, persists to
  `eval_runs`/`eval_results`. **A/B** (`ab.py`) diffs two configs per question. **Report-level**
  (`report.py`) and **feedback→golden triage** (`feedback.py`) round out §18.8/§18.9.
- **Gate / EVAL-002** (`gate.py`): fails a PR if Recall@5 drops > 2 pts, citation accuracy > 1 pt,
  or hallucination rises > 0.5 pts vs `app/eval/baseline.json`. Enforced in CI as a pytest
  integration test (`tests/integration/test_eval_runner.py`) that also proves a deliberate
  citation-accuracy regression is caught; the full set runs nightly via `scripts/run_eval.py`.

Only the deterministic metrics (numeric accuracy, abstention, citation accuracy, hallucination) are
meaningful in mock mode; Recall@K and judge faithfulness need live Voyage + an LLM key
(ADR-0009/0010/0020). The README benchmark table marks those _pending_.

### Published numbers and their gate (Phase 7l, NFR-ME-001 … 006)

`make eval-report` builds a throwaway database (`assay_desk_eval`), ingests the seed companies
from fixtures, runs `golden_v0` and the injection corpus through the real chat pipeline with the
deterministic FakeLLM, times report generation, and writes `docs/eval/latest.{json,md}`. Every
proportion has its count and a Wilson 95% interval (`app/eval/published.py`). Citation validity
and number match are re-checked on the rendered answer against the source resolver, not by
re-running the verifier being measured. `docs/eval/thresholds.json` holds the floors.
`test_the_committed_report_passes_its_own_gate` (and `make eval-gate`) fails CI on a breach, or
when the report's `inputs_hash` no longer matches the chat, citation, retrieval, prompt, eval
and golden-set files it measured. The hash is over content, not commits, so a report committed
alongside its code stays fresh. `EVAL_LIVE=1 make eval-report OUT=live` is the manual live-model
run: it writes `docs/eval/live.{json,md}` with the same metrics plus the run's cost by purpose
from `llm_calls`, and is not gated.

### Docs consistency (Phase 7l, NFR-ME-011)

`scripts/check_docs_consistency.py` runs in `make check` (and as a unit test). It fails on:
- a requirement ID cited in tests or docs that is missing from the spec;
- a README route missing from `openapi.json`;
- a README number that is in neither `latest.json` nor `docs/generated/stats.json`;
- a duplicated or skipped ADR number;
- a task file in `00_START_HERE.md` that does not exist;
- stale generated stats or README blocks (`make stats`).

Each rule has a failing-case test in `tests/unit/test_docs_consistency.py`.

## Tier 2 — E2E + accessibility (`e2e/`)

The §30.3 happy path: **search NVDA → load dashboard → generate report → ask question → receive
citation → open source**, plus an axe scan asserting no serious/critical WCAG 2 A/AA violations.

```
# with the stack running and seeded (API on :8000, frontend on :5173):
cd e2e && npm install && npm run install-browsers && npm test
```

Or run `e2e.yml` via "Run workflow". A full run needs a completed report + chat, which require an
LLM key (ADR-0006/ADR-0009) or the Fake-LLM seed.

The suite has five behavioural specs, a visual regression spec, and the capture script:

| Spec | What it covers | Needs |
|------|----------------|-------|
| `tests/happy-path.spec.ts` | The §30.3 happy path (search, company page, research report, a question in the assistant panel, its citation, the source) and the axe scan of the company page. Creates its own account through the sign-in form. | A completed research report for the ticker. The question is a figure question, which mock mode answers deterministically with a citation. The axe test alone needs only the seed companies. |
| `tests/shell.spec.ts` | The app shell (UI plan Step 2): command palette shortcuts and §9.2 disambiguation, the five company tabs and old-URL redirects, the pinned sub-header, the outage banner and its recovery, no horizontal scroll at 360 px, theme switch, sign-in validation and return-to-page. | Seed companies only. Creates a few throwaway `e2e-…@example.com` users per run. |
| `tests/company.spec.ts` | The company page (UI plan Step 3): the price hero (range changes without refetching; scrubbing by mouse, touch, and arrow keys with no layout shift), key figures, the no-price and not-applicable cases, chart table views and CSV downloads, theme-following charts, Financials controls, statement lineage tooltips, and 360 px fit. | Seed companies, plus one company with no price history (AMD in the dev database). |
| `tests/reading.spec.ts` | The reading surfaces (UI plan Step 4): the citation panel (opens beside the text, swaps, returns focus, closes when the reader moves on), the research document (one AI mark, contents, every claim cited, risks filter, old Risks/Management links), the filing comparison (summary sentence, inline word diff, both filings' passages, the 404 and 422 cases), the news feed (day groups, clustered stories, filters), unsafe URLs in API data never becoming links, the assistant panel (docks at 1440 px, survives tab changes, cites, declines, one panel only, keyboard), phone layouts at 360 px, and an axe scan of all of it in both themes. | The dev database's NVDA filings (ids in the spec). The research report is mocked from `e2e/fixtures/research-nvda.json`, whose source ids are real and resolve against the API; dev databases only hold FakeLLM placeholder reports. Creates one throwaway user. |
| `tests/tools.spec.ts` | The tools and personal features (UI plan Step 5): Compare (the strongest figure per row, N/A with its reason, a shareable and defensively parsed URL, adding a company through the search palette, the chart with its table and CSV, the sign-in-gated AI explanation that is never shown uncited), Portfolio (weights recompute with no network request, the Normalize action, missing-data sentences, cited risk themes), the watchlist, alerts and notification bell, the happy path by keyboard alone, reduced motion, 40 px touch targets on every main screen, 360 px fit, and an axe scan of Home, Overview, Financials, Compare, Portfolio and their popovers in both themes. | Seed companies NVDA, AMD, INTC, AAPL and JPM. Creates throwaway users. The notification itself is stood in for with a mocked response, since alerts are dispatched by a batch job. |
| `tests/visual.spec.ts` | Visual regression: nine key screens, dark and light, at 1440 px and 390 px, compared with committed baselines (`toHaveScreenshot`, 1% tolerance). Run with `npm run visual`; it is tagged `@visual` and left out of `npm test`. | Only the frontend. API responses are replayed from `fixtures/visual.har` and the clock is fixed, so it does not depend on the database or the date. Baselines are per operating system; the committed ones are macOS. Re-record responses with `npm run visual:record` (dev stack running); accept an intended change with `npm run visual -- --update-snapshots`. |
| `tests/screenshots.spec.ts` | Not assertions: writes full-page captures of every key screen, dark and light, at 1440 px and 390 px, to `e2e/screenshots/` for visual review. Run with `npm run screenshots`. | Seed companies and the dev server (`/_ui` exists only there). NVDA's research report is the same fixture. The last few captures are taken signed in, with a throwaway account. |

Run one spec with `npx playwright test tests/shell.spec.ts`.

The suites run in parallel (Playwright's default workers). They used to need one worker because
overlapping requests could stall the API; ADR-0029 fixed that, and a stall here is now a bug.

### Vertical slice (Phase 7l, NFR-ME-012)

`scripts/vertical_slice.sh` rebuilds the demo database (`make demo-db`) and serves the demo build
on ports 8010 and 5180, leaving a dev stack on 8000/5173 alone: a `DEMO_MODE` API and the
production frontend build. It smoke-tests the API, then runs `e2e/tests/vertical-slice.spec.ts`:
1. NVIDIA's overview (quarterly/TTM, red flags, earnings quality);
2. the recorded cited answer and its source;
3. the earnings brief and guidance vs actual;
4. a thesis KPI that breaks and notifies.

The spec fails on any console error. Its frames become `docs/media/demo.gif`
(`scripts/make_demo_gif.py`).

### Lighthouse (UI-004)

Measured 2026-10-11 with Lighthouse 12.8 on the production build (`npm run build && npm run
preview`), API on localhost in mock mode. Target: 90 or above for Performance and Accessibility.

| Page | Preset | Performance | Accessibility | LCP | CLS |
|------|--------|-------------|---------------|-----|-----|
| Home | desktop | 100 | 100 | 0.7 s | 0 |
| Home | mobile (slow 4G, 4× CPU) | 94 | 100 | 2.8 s | 0 |
| `/company/NVDA` | desktop | 98 | 100 | 1.1 s | 0.001 |
| `/company/NVDA` | mobile (slow 4G, 4× CPU) | **76 to 79** | 100 | 5.2 s | 0.001 |

The mobile company page misses the performance target. Its layout shift was fixed (0.76 before
loading states were shaped like their content; 0.001 now), which took the score from 55. What is
left is the largest paint, the price hero, which waits on three stages: the entry bundle, the
company and Overview chunks with the chart library (100 kB gzip), and the price history. That
last response is 179 kB because the API does not compress responses; behind a proxy or with
compression enabled it would be about a tenth of that. Compressing API responses is the next
step and is a backend change. Requesting the route's chunks early was tried and reverted: it
delayed first paint by 0.5 s and left the largest paint unchanged.

Reproduce: `CHROME_PATH=<a Chrome binary> npx lighthouse http://localhost:4173/company/NVDA
--only-categories=performance,accessibility` (add `--preset=desktop` for the desktop rows).

## Tier 3 — Load (`load/locustfile.py`)

Drives the hot read endpoints (search, company, financials) and, with a token, authenticated chat,
to check the §8 latency targets. **Run locally on 2026-11-22, not yet against a deployed stack** —
the Tiingo free licence blocks a public deploy until swapped (ADR-0007).

Setup for the rows below: Apple M1 (8 cores, 16 GB), one uvicorn process as in production
(`backend/Dockerfile`), Postgres 16 + Redis in Docker, Locust on the same machine; dev database
(five companies); `CACHE_ENABLED=true`, `LLM_PROVIDER=mock` (so chat times exclude model time and
spend no credit), real Gemini query embeddings. Profile: 50 users (25 read, 25 chat) spawned at 5/s
for 2 minutes, about 23 requests/s, 0 failures. Retrieval p95 comes from `retrieval_logs`. Code at
`2403621` plus ADR-0068.

```
uv run --with locust locust -f load/locustfile.py --host http://localhost:8000 \
    --users 50 --spawn-rate 5 --run-time 2m --headless
# set ASSAY_DESK_TOKEN=<bearer> to include the chat path
```

### NFR targets (§8) and results

| Metric | Target | p95 measured | Date / notes |
|--------|--------|--------------|--------------|
| `GET /companies/{ticker}` (cache hit) | < 150 ms (NFR-001) | **120 ms** (12 ms reads only) | 2026-11-22, local, mixed load ✓ |
| `GET /companies/{ticker}` (miss) | < 500 ms (NFR-001) | _not isolated_ | only the first request per ticker misses with the cache on |
| `GET /companies/search` (cache) | < 300 ms (FR-001) | **170 ms** (18 ms reads only) | 2026-11-22, local, mixed load ✓ |
| `GET /companies/{ticker}/financials` | — | 190 ms (24 ms reads only) | 2026-11-22, local, mixed load |
| retrieval (chat) | < 400 ms (NFR-002) | **1,453 ms** under load; 662 ms single user | ✗ about 320 ms of it is one Gemini query-embedding round trip from the test machine |
| chat answer (mock LLM) | < 3 s (NFR-004) | **2.6 s** | full verified turn without model time; a real model adds its own latency. One 61 s outlier in 590, unexplained |
| report generation | ≤ 90 s (NFR-003) | p50 59–61 s single user; JPMorgan 51 s, Intel 32 s at `LLM_RATE_LIMIT_PER_SEC=2.0` | live eval (`docs/eval/live.md`) at the default 0.25; JPMorgan takes 109 s at 0.25 (2026-11-22), not under load ✓ with a paid-key pace |

The first run found, and ADR-0068 fixed: the default span exporter printing every span inside the
request (read p95 410–450 ms → 12–24 ms read-only); chat turns holding the shared DB slots for
seconds, so reads queued and 2% of all requests got 503s (chat now has its own 6 slots: read p95
700 ms → 120–190 ms under mixed load, at the cost of chat queueing behind other chats); and four
sequential query embeddings per chat question (now one call: chat p50 3.0 s → 1.2 s). Ten chat
slots instead of six brought chat p95 to 2.8 s but put read p95 back at 500–600 ms (CPU contention in
one process), so six it is; chat capacity scales with API instances. The locustfile also sent
`ticker` instead of `company` to `/chat`, `period_type=annual` instead of `FY` to `/financials`, and
stopped the whole run when no token was set; all three are fixed.

Record each run here (replace _not yet run_ with the measured p95 + commit SHA + hardware) so the
NFR claims in the README benchmark table (§31) are backed by data, not assumed.
