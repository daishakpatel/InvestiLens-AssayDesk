# Assay Desk — Remaining Work

Audited 2026-10-12 against `docs/AssayDesk_Spec_v2.md`; last updated 2026-10-29 (backlog pass). All
task files through `37_phase7l_measured_results_and_polish.md` and the 5 `UI_plan/` steps have
been worked through; this is what is still open. `docs/limitations.md` keeps the reader-facing
"known issues" table in step with this list.

Tags: `[code]` = checked in the code, database or CI on the date shown in the item or, if none,
the audit date. `[notes]` = carried from earlier session hand-offs, not re-checked.

## State on 2026-10-14

- CI on `develop` is green at `0b3cc01`.
- **Not yet committed:** stored valuation metrics (ADR-0036), readable figures in AI reports
  (prompt `report_v2`), the frontend's handling of multiples, their tests and docs. All 516
  backend tests and 77 frontend tests pass with them; finance and citation coverage is 100%.
- Gemini billing is on ($10 credit added 2026-10-14). Local `.env`: `LLM_PROVIDER=gemini`,
  `LLM_MODEL_STRONG=gemini-3.5-flash-lite`, `EMBEDDING_PROVIDER=gemini`, `PRICE_PROVIDER=tiingo`,
  `REFRESH_PRICES_ON_STARTUP=true`. News is still sample data.
- Dev database: five companies (NVDA, AAPL, JPM, AMD, INTC) with real SEC financials, real prices
  and stored valuations. Filing text is parsed and embedded for all five (19,808 passages).
  Fresh research reports for JPM, AAPL and INTC (2026-10-15, `gemini-3.8-flash`); NVDA's and AMD's
  are from earlier the same day.

## 0. Broken right now

- [ ] **JPM's 10-Q has no Part I item headings** (cross-reference layout): its financial
      statements and MD&A are kept as one "Part I" section (`10q_part1_unitemized`) and flagged
      low-confidence, not split into Item 1 / Item 2. `[code]`

## 1. Decisions waiting on the project owner

- [ ] **Commit** the uncommitted work listed above.
- [ ] Product sign-off on AI budget placeholders ($5/user/month, $0.50 in demo, $25/day global). `[notes]`
- [ ] Form 4 / 13F in scope or not (spec Appendix F #10). `[notes]`

## 2. MVP Definition of Done (spec §36) — not yet met

### AI answers and reports

- [ ] **News summary is empty in every report**: news is still sample data. `[code]`
- [ ] **Report sections search with fixed keywords and no preference for recent filings**
      (`_SECTION_QUERY` in `backend/app/research/report.py`): NVDA's bull case picked up crypto
      "mining demand" from older 10-Ks, and AMD's management section quoted forward-looking-
      statement boilerplate. `[code]`
- [ ] **Price questions go to document search**: "what was the stock high on Sep 29" or "stock price
      today" are not answered from `price_history` (prices have no citable source type yet). One
      such question sent 23k tokens of context, well over the 6,000-token budget. Plain "margins"
      is not in the metric lexicon, so "how did margins trend?" goes to the filings. `[code]`
- [ ] **Report speed**: about 70 s on `gemini-3.5-flash-lite` (18 calls), 171 s on `gemini-3.8-flash`
      with free-tier pacing, against a 90 s target (NFR-003). `LLM_RATE_LIMIT_PER_SEC` (0.25) can be
      raised now that billing is on. `[code]`
- [ ] Comparison commentary and filing-diff adjudication have never run on a real model. `[notes]`

### Measured numbers (§36 #20)

- [x] Mock-run numbers with Wilson intervals are published and gated → 37 (`make eval-report`,
      `docs/eval/latest.md`, `thresholds.json`).
- [ ] **Publish live-model numbers**: Recall@5 with real embeddings, judge faithfulness, cost
      and latency per report/chat. `EVAL_LIVE=1 make eval-report OUT=live` exists and is
      untried; about 106 questions, roughly $1–2. `[code]`
  - [ ] Judge calibration ≥ 0.85 against a 50-question human-labelled sample (none committed). `[notes]`
  - [ ] Re-tune `rag_sufficiency_min_score`, entailment and confidence thresholds on real
        embeddings; decide reranker (ADR-0014) and entailment verifier (ADR-0015). `[notes]`
- [ ] **Load test never run**: every row of the NFR table in `docs/testing.md` says `_not yet run_`. `[code]`

### Data

- [ ] **Valuation uses fiscal-year figures, not TTM** (ADR-0036): P/E late in a fiscal year uses
      earnings up to ten months old. TTM EPS, revenue, FCF and EBITDA are stored since Phase 7b
      (ADR-0040, `period_type` TTM); switching `valuation_builder` to them is Phase 7h. `[code]`
- [ ] **AMD pretax income and AMD/Intel D&A are not mapped quarterly**: their tags are missing
      from the concept map, so `metric_gaps` lists them and their TTM effective tax rate, ROIC
      and EBITDA are absent (checked 2026-10-21 on the dev DB). `[code]`
- [ ] **AMD and Intel have no total liabilities tag** (Phase 7d): their Altman Z is
      `insufficient_data` (missing `total_liabilities`), and so are their balance-sheet accruals.
      Deriving it as assets − equity was rejected because of temporary equity (ADR-0042). Altman's
      original Z also needs a fiscal-year-end price, so years before the stored price history
      are insufficient. `[code]`
- [ ] **Peer percentiles compare latest fiscal years, not calendar years** (Phase 7d): NVIDIA's
      January year sits beside December years; the comparison engine's calendarization is not
      used here. `[code]`
- [ ] **Quarterly ratios are not stored** (quarterly gross/operating margin, ROE): only TTM
      ratios are (Phase 7b scope). The Financials Quarterly view therefore hides the Margins
      charts. `[code]`
- [ ] **TTM EPS right after a split**: the identity takes each term's latest restatement, but a
      split after the last 10-K and before any filing restates the prior year still mixes share
      bases; the `share_count_changed` flag marks such windows (ADR-0040). `[code]`
- [ ] **Five-year average multiples** (spec §10.7 "5Y Average P/E"): Phase 7h stores each
      multiple at the last five fiscal year ends (point in time) and serves min/median/max in
      `/valuation-bands`; the research report payload does not use them yet. `[code]`
- [x] **EV/EBITDA is N/A for AMD and Intel**: fixed in Phase 7h (ADR-0046). Neither tags a total
      D&A any more, so company overrides sum the parts they do tag (Intel FY2025 $11,706M, its
      statement's own lines; AMD FY2025 $3,050M, $46M of non-acquisition intangible amortization
      counted twice because AMD's statement line uses its own tag). AMD tagged no D&A before 2022,
      so its EV/EBITDA for those year ends stays `missing_input`. `[code]`
- [ ] A latest-close valuation is replaced on every price run, so a report that cited yesterday's
      dated ID (`derived:NVDA:pe_ratio:<date>`) no longer resolves it. `[code]`
- [ ] **Seed universe is 5 of 10 in dev, 3 in fixtures** (fixtures: NVDA, AAPL, JPM). Missing
      MSFT, AMZN, TSLA, GOOGL (multi-class shares), META, V, MA (§4.1). `[code]`
- [ ] **News is sample data** (placeholder headlines); live news needs a Finnhub key, and a
      `NEWS_PROVIDER` switch like the ones for the LLM, embeddings and prices. `[code]`
- [ ] **New SEC filings arrive only when ingestion is run by hand** locally. Prices catch up at
      API start; filings have no equivalent. `[code]`
- [ ] Prices refresh only at API start, and the check is "older than 12 hours", not "has the
      market closed since". Fine for local use; a hosted deploy uses the `price-eod` schedule. `[code]`

### Accounts and user features (§26.2, §36 #24)

- [ ] **Saved reports / research history / saved questions**: reports carry a `user_id` but
      there is no list endpoint and no UI. `[code]`
- [ ] **Watchlist items**: no add/remove-item endpoint; each watched company is its own
      watchlist (ADR-0031 stand-in). Needs `POST/DELETE /watchlists/{id}/items` + a data migration. `[code]`
- [x] Alerts: `POST /alerts` takes no `config` (price-move threshold, news category);
      `Notification` has no ticker. Done in Phase 7a (ADR-0039): typed `params`, `ticker` on
      notifications, `/alerts` settings page. `[code]` / `[notes]`
- [ ] Email: no delivery provider for verify-email / password-reset; `email_verified_at` gates
      nothing; SMTP backend unproven. `[code]` / `[notes]`
- [ ] Alerts (Phase 7a leftovers): `filing:<id>` / `price:…` evidence ids don't resolve via
      `GET /sources/{id}` (the UI maps them to routes); email is sent before the dispatch job
      commits, so a crash in that window can resend one (ADR-0039); `price_move` checks only the
      newest bar per company. `[code]`

### Report-level features (§10.16)

- [ ] `GET /research/{id}/export` is 501 (Markdown export and print are client-side only). `[code]`
- [ ] `GET /research/{id}/diff` ("what changed since last report") is 501. `[code]`
- [ ] Shareable read-only links: not built. `[code]`
- [ ] Rejected-claims admin view: not built (data is logged in `claim_verifications`). `[notes]`
- [ ] `POST /feedback/report` is a 204 no-op (`backend/app/api/watchlist.py`). `[code]`

### Deployment and tests

- [ ] **Hosted demo** (§36 #23): built but not deployed → 37 (ADR-0050). `DEMO_MODE` is wired
      (fixtures, recorded answers, shared sandbox, IP limits, banner, `/demo`), `render.yaml` is
      the ~$30/mo demo blueprint, and `scripts/vertical_slice.sh` passes on a local demo build.
      Remaining owner steps: create the Render Blueprint, then set the `RENDER_DEPLOY_HOOK_URL`
      secret and the `DEMO_API_URL` variable (docs/deploy-demo.md). The DoD's "deliberately
      broken branch in the PR description" needs a PR. `[code]`
- [ ] **E2E is manual-only** (`e2e.yml` is `workflow_dispatch`); Linux visual baselines are not
      committed; behavioural specs assume the dev DB, not the CI seed. `[code]` / `[notes]`

### Before any public deploy

- [x] Request bodies are capped (256 KiB, `413`/`411`) → 37; thesis text bounded → 36. Individual
      fields (`password`, `FeedbackRequest.reason`, `WatchlistCreate.name`, search `q`) still have
      no per-field `max_length`, but no body can exceed the cap. `[code]`

## 3. Spec features not built (P2)

- [ ] **Insiders & ownership** (§10.15): no Form 4 / 13F ingestion; endpoints return `[]`; the
      `ownership` route redirects to overview. `[code]`
- [ ] **DCF and scenario tools** (§37.6): nothing exists. `[code]`
- [ ] **ROIC (annual) and revenue CAGR are still unpersisted** (§37.7 leftovers): Phase 7d
      stores the earnings-quality measures (below, "Done") and 7b stores TTM ROIC, but no annual
      ROIC or multi-year CAGR row exists. `[code]`
- [ ] **Consensus estimates** (§37.3, ADR-0047): "beat/miss vs Street" and surprise
      percentages need a licensed estimates source; none exists, so only company guidance is
      compared (Phase 7i). Transcripts are deferred (ADR-0011). `[decision]`
- [ ] **Report analytics** (§10.3–10.7): revenue waterfall, seasonality, segment/geo mix
      (dimensional XBRL), margin bridge, valuation percentile bands. `[notes]`
- [ ] **RAG / AI**: semantic cache (RAG-050); cheap-model intent classifier and query rewriter;
      `get_filing_diff` and `get_insider_transactions` tools; conversation-aware rewrite;
      true incremental token streaming. `[code]` / `[notes]`
- [ ] **Citations**: numeric auto-repair (reject-only today); NLI / LLM-judge entailment. `[notes]`
- [ ] **Documents**: PDF parsing (DP-005); iXBRL cross-check not wired into the pipeline;
      anomaly identity/YoY checks not wired; no endpoint for full in-app filing text. `[code]` / `[notes]`
- [ ] **News**: tab shows 25 items with no paging; categorizer is rule-based; no sentiment
      (spec marks it optional). `[notes]`
- [ ] Filing diff is recomputed per request, not cached. `[notes]`
- [ ] §37.8 P3 candidates (screener, sector dashboards, Excel export, …): untouched by design.

- [ ] **Red-flag phrase rules have no real positive examples for going concern or material
      weakness** (Phase 7e): none of the five dev companies reports one, so those rules are
      tested on labelled synthetic sentences. Add a real 10-K with each once such a company is
      ingested, and check the guards against it. `[code]`
- [ ] **Red flags quote one paragraph per rule per filing**: a filing with two separate
      departures shows one flag (with all roles from the departure sentences); the second
      person's sentence is not quoted separately. `[code]`

- [ ] **Footnotes leftovers** (Phase 7f, ADR-0044): Intel's note headings are one-row tables
      ("Note 1 :" | "Basis of Presentation"), not text, so INTC has no notes (flagged
      low-confidence); JPM's FY
      `total_debt` holds short-term borrowings only, so its lease-adjusted debt understates; the
      last note runs to the end of its section (it absorbs NVIDIA's Schedule II / exhibit text);
      JPM note titles that wrap ("Off–balance sheet lending-related") keep the first line only and
      fall to `other`; page headers ("Notes to the Consolidated Financial Statements") count as
      passages. Summaries only for the latest 10-K, not 10-Qs; none are cached in the dev DB since
      the v5 re-parse (`make footnote-summaries`, a few cents). `[code]`

- [ ] **Governance leftovers** (Phase 7g, ADR-0045): board counts are often not stated in
      prose (Apple, JPMorgan give them only in nominee tables/graphics), so the independence
      ratio is mostly "not stated"; a deterministic count from the nominee table would fix it.
      AMD's pay ratio is not found (median pay sits outside the ratio sentence). Intel's FY2024/
      FY2025 PvP is tagged per CEO (dimensioned) and not read. Proxy section headings are
      keyword-matched, so some sections carry neighbouring text (NVIDIA's board size sits under a
      say-on-pay heading). The Gemini client rejects JSON followed by extra text, so an occasional
      `governance_v1` call fails and is retried on the next run. `ecd:PeoName` is matched to the
      table by name words; a CEO change within one year shows one PEO. `[code]`

- [ ] **Peer-set and band leftovers** (Phase 7h, ADR-0046): with five dev companies every
      default peer set is "insufficient peers" (NVIDIA's same-SIC peers are 6-12x smaller), so peer
      statistics need a user's own set until the universe grows; NVIDIA's FY2021 capex is not in
      companyfacts, so its FY2022-FY2024 EV/FCF and FCF yield are `missing_input`; price to tangible
      book for banks needs goodwill/intangibles mapped (P/B is N/A for JPM); the 200x
      not-meaningful cutoff is a judgement call; a user set's members beyond five are not all shown
      on Compare (six-company limit, stated on the page); the earnings-quality peer percentiles
      (Phase 7d) still compare latest fiscal years rather than the TTM basis used here. `[code]`

- [ ] **Guidance leftovers** (Phase 7i, ADR-0047): outlooks set out as a table (Intel's) are not
      read, so Intel has no guidance; "17.0%, plus or minus 1%" tax-rate guidance (NVIDIA through
      Q4 FY2026) is rejected as ambiguous (points or relative?); single-figure guidance
      ("approximately $9.2 billion") compares literally, so any difference is above or below; EPS
      guidance without a stated basis is never compared; the raw model response is not kept, so a
      verification-rule change means clearing `documents.metadata.guidance_extraction` and paying
      for the calls again; AAPL/JPM releases have no numeric outlook; `avg_delta_vs_mid` is
      returned at full Decimal precision (display rounds it); the track record covers revenue
      only. `[code]`

- [ ] **Earnings brief leftovers** (Phase 7j, ADR-0048):
  - The per-user regeneration limit is in-memory, per API process; it needs the Redis limiter
    once the API runs on more than one process.
  - Announced dates are read only from ingested 8-Ks and exhibits. Companies that post the date
    only on their IR site get an estimate (all five dev companies do today).
  - A price-driven input (the 5-session move filling in, the valuation date rolling) makes a new
    version on the next user refresh or daily preview run.
  - `scripts/build_metrics.py` does not make briefs (it would cost model calls).
  - Transcripts and Q&A themes are out of scope (ADR-0011).
  - The full e2e suite was not re-run, only `earnings.spec.ts`.
  - Visual baselines were not updated for the new tab.
  `[code]`

- [ ] **Thesis tracker leftovers** (Phase 7k, ADR-0049):
  - The draft limit is in-memory, per API process (the same caveat as briefs).
  - The metric registry is curated, so a new trackable metric is a code change; there are no
    user-defined formulas.
  - The markdown subset has no tables or nested lists.
  - A threshold edit that flips a KPI's state never notifies; it is a baseline by design.
  - `revenue_vs_guidance` reads only revenue headline outcomes.
  - The full e2e suite was not re-run, only `thesis.spec.ts` and `earnings.spec.ts`.
  - Visual baselines were not updated for the new tab and page.
  `[code]`

- [ ] **Measured results and demo leftovers** (Phase 7l, ADR-0050):
  - The live demo URL, its CI smoke run, and the "deliberately broken branch" PR demonstration
    are owner steps (Render account, repo secrets, a PR). The gate's failing cases are covered by
    `tests/unit/test_eval_published.py`.
  - Demo answers replay recordings keyed by exact prompt; any prompt or seed-order change makes
    them miss (honest "not enough evidence" fallback). `make record-demo` re-records them (live
    calls, a few cents).
  - The demo's banner date is the latest fixture filing; fixture prices run later than that.
  - The API and frontend sit on different `onrender.com` subdomains, so a reload ends a demo
    session (SameSite=Strict cookie); a custom domain fixes it.
  - Latency in `latest.md` is single-user and mock-LLM; the locust load test is still unrun.
  - README stats count test functions, not parametrized cases.
  - Existing signed-in browsers see one extra sign-in after deploy (the new `il-session` hint
    starts empty).
  - The full e2e suite and visual baselines were not re-run; only `vertical-slice.spec.ts`.
  - `filings/service._emphasized` always reads the default object store, not the store a caller
    ingested into. That is harmless in production (one store), but it made
    `test_brief_versions_follow_the_filings` pass only when `backend/.storage` was empty. The test
    now pins its synthetic filing to guessed captions; passing the storage through would be the
    real fix.
  `[code]`

## 4. Never verified live

- [ ] Finnhub (news) and Voyage (embeddings) paths: no API keys; parsing is unit-tested only. `[notes]`
- [ ] Prometheus / Grafana dashboards, OTLP exporter, MinIO object storage. `[notes]`
- [ ] The Celery Beat schedule and the full daily pipeline. `[notes]`
- [ ] Light theme screen by screen; real print preview; no toast system. `[notes]`
- [ ] `app/tasks/` wrappers are mostly 0% covered. `[notes]`

## 5. Housekeeping

- [ ] Definition of Done checkboxes in task files `01`–`25` were never ticked (every box is
      `[ ]`), although each phase was handed off as done. `UI_plan/` steps are ticked. `[code]`

## Done since the audit

- 2026-10-12 — **CI green**: an "offline" contract test needed a seeded database; the guard test
  for ADR-0029 checked no routes and hid an `async` `/metrics` handler; a Dependabot major bump
  (TypeScript 7) broke `npm ci`; the pinned Trivy release had been deleted. Dependabot no longer
  groups major versions.
- 2026-10-12 — **Live LLM**: Gemini behind `LLMClient` (ADR-0032), with `LLM_PROVIDER=gemini`.
  The root `.env` is now actually loaded by the backend, and tests ignore `.env` entirely.
- 2026-10-12 — **Chat answers**: keyword search falls back to any-word matching; the Gemini
  client rewrites the model's citation styles into `[SOURCE:id]`.
- 2026-10-12 — **Real embeddings**: Gemini `gemini-embedding-2` behind `EmbeddingClient`
  (ADR-0033). Vector search is limited to the active model; retrieval falls back to keyword
  search if the provider fails; `scripts/embed_chunks.py` is resumable.
- 2026-10-12 — **Readable numbers in chat** and the citation panel
  (`backend/app/finance/display.py`): "$215.94 billion", "71.07%".
- 2026-10-13 — **Net income / operating income answered with revenue**: every stored metric now
  has its own `source_id` (ADR-0034); dev metrics rebuilt (3,209 rows, 3,209 distinct IDs). The
  same change fixed a 500 from `GET /sources` for derived fourth-quarter figures.
- 2026-10-13 — **Real prices**: `PRICE_PROVIDER=tiingo` (ADR-0035), all five companies loaded
  2021-10-14 to 2026-10-13, sample data removed, and prices caught up at API start with
  `REFRESH_PRICES_ON_STARTUP=true`.
- 2026-10-13 — A real research report exists for NVDA in the dev DB (`gemini-3.5-flash-lite`).
- 2026-10-14 — **Embeddings complete**: all 7,722 parsed chunks embedded with Gemini on the paid
  tier (about 1.7 million tokens in total, roughly $0.35).
- 2026-10-14 — **Market cap, EV and multiples stored** (ADR-0036): per fiscal year and at the
  latest close, rebuilt with every price run, split-adjusted, bank EV multiples N/A. They fill
  the header, the Financials valuation panel, comparisons, peer market-cap bands and the report's
  valuation section.
- 2026-10-14 — **Readable figures in AI reports**: the report payload and evidence carry figures
  as "$215.94 billion" / "71.07%" / "46.63x" (prompt `report_v2`); a fresh NVDA report had no raw
  numbers in its 26 figure-bearing claims.
- 2026-10-14 — **Chat answers on AMD** (user report): Markdown (`*`, `**`, `###`) is removed before
  the answer is checked and the model is told not to write it; "risks"/"margins" now count as
  factual terms, so a cited answer is no longer declined because only its uncited sentence looked
  factual; a short question is no longer glued to the previous one unless it opens with a
  follow-up word or leans on a pronoun ("what does management attribute it to?"); semicolon-
  separated citations are split.
- 2026-10-15 — **JPM questions declined** (user report): JPM's and Apple's filings had only come
  from the sample set. Loaded five years from EDGAR, fixed the loader skipping a known filing whose
  stored document was missing (ING-001), parsed and embedded them (5,342 passages, about $0.16).
  `scripts/parse_filings.py` now takes tickers so other companies keep their embeddings.
- 2026-10-15 — **Trend questions answered from stored figures**: "How has revenue grown over the
  last three years?" (a "how", not a "why", naming a stored metric over several years) is answered
  from `financial_metrics` with year-on-year growth, for every company.
- 2026-10-15 — **Retrieval cap no longer starves single-filing companies**: the 3-per-filing cap
  only reorders; it fills from the same filing when no other filing can.
- 2026-10-15 — **Research reports for JPM, Apple and Intel** (user report): JPM's and Apple's were
  fake-model placeholders from 2026-10-11 and Intel had none; all three regenerated. The report
  payload now covers the last five fiscal years, says "not applicable for this sector" once
  instead of per year, leaves out metrics with no value, and adds ROE/ROA to profitability, so a
  bank's profitability section discusses net margin and returns. A 10-K with no "Item" headings
  (Intel) is now kept as one flagged section instead of being dropped (6,744 passages).
- 2026-10-21 — **Quarterly and TTM metrics persisted** (Phase 7b, ADR-0040): the last 12 fiscal
  quarters of every flow metric and balance, cash flows de-cumulated from YTD facts (the derived
  Q4 operating cash flow had never been produced), every complete TTM window with TTM margins
  and ROIC, and a `metric_gaps` table behind `GET /companies/{ticker}/metrics/coverage`.
  `/financials` takes `limit` and returns each point's source ID and formula; the Overview has
  an Annual / Quarterly toggle (quarterly bars, TTM line). Dev metrics rebuilt for all five
  companies. Caught on the way: TTM EPS mixed pre- and post-split figures (NVIDIA read -2.28),
  and `get_latest_metrics` would have let a quarter of the year in progress displace the annual
  figure in portfolio and research.
- 2026-10-22 — **SEC exhibits, extra forms and the 8-K item fix** (Phase 7c, ADR-0041): 8-K items
  are full dotted codes (5.02 and 5.07 were both "5"); 10-Q Part I (financial statements, notes,
  MD&A) is chunked again (Part II's item numbers had overwritten it, so it was never chunked);
  EX-99.1/99.2 of earnings 8-Ks are stored as tier-2 `earnings_release` documents; DEF 14A,
  DEFA14A, NT 10-K/NT 10-Q (`late_filing_notice`) and 8-K/A are ingested with per-form backfill
  windows; older submission pages are read, so JPM now has five years of 10-Ks and 10-Qs; the
  parse step skips current documents and keeps embeddings of unchanged text; `make reparse-8k`.
- 2026-10-22 — **Earnings-quality dashboard** (Phase 7d, ADR-0042): the Beneish M-score (8
  variables, −1.78 threshold, checked against a second source), the Altman Z variant chosen by SIC
  (original or Z″, stored with the result), Piotroski with all nine criteria, accruals
  (cash-flow and balance-sheet), DSO/DIO/DPO/CCC, FCF conversion and split-safe dilution. These
  are persisted per fiscal year with resolvable inputs, plus TTM FCF conversion and accruals.
  `GET /companies/{ticker}/earnings-quality` serves them with peer percentiles (hidden below
  three peers). The new Earnings quality tab has five cards, the caveat strip, a cash-vs-earnings
  chart, a five-year table and an inputs disclosure that opens the citation drawer. The verifier
  rejects fraud or manipulation wording on a cited score (`report_v3`). Caught on the way:
  NVIDIA's FY2026 marketable securities moved to a new tag, and the missing value would have
  been read as zero; NVIDIA's FY2022 dilution read +304% because the 2021 split predates the
  price history (now taken from the 10-K's restated comparative); derived lineage stored no input
  values.
- 2026-10-22 — **Red-flag panel** (Phase 7e, ADR-0043): a rule registry,
  `red_flag_taxonomy_v1`, with 8-K items 1.03, 1.05, 2.05, 2.06, 3.01, 4.01, 4.02, 5.01 and 5.02
  checked against SEC Form 8-K (SEC 873, 02-25), NT 10-K/10-Q (Form 12b-25), guarded going-concern
  and material-weakness phrase rules, Beneish above threshold and restated XBRL values. Flags are
  stored in `red_flags` (versioned, NULLS NOT DISTINCT unique key) after parsing, nightly and via
  `make redflags-backfill`, cited as `redflag:` IDs, and served by
  `GET /companies/{t}/red-flags` and `GET /red-flags?watchlist=true` with a scanned-through date.
  A `red_flag` alert type was added, and the Overview has a Red flags section with a count on the
  tab. Caught on real 8-Ks: "Mr." split sentences, "Vice President" counted as President, award
  terms and director bios read as departures, and a neighbouring sentence's role misattributed.
- 2026-10-23 — **Footnote extraction** (Phase 7f, ADR-0044): notes segmented at parse time
  (`sec-ingest-v3` starts a chunk at each note heading; `documents.metadata['notes']`),
  classified by title (`note_taxonomy_v1`); XBRL-first footnote facts (`footnote_metrics`,
  `footnote_builder`) with inline-XBRL fallback for the latest 10-K (NVIDIA's ladder from per-note
  dimensioned facts), `debt_due_within_2y_share` and `lease_adjusted_debt`, gaps never zero;
  `note_summary_v1` bullets kept only with a verbatim quote and matching numbers, cached per
  (note, prompt, model), generated after parsing or by `make footnote-summaries`;
  `GET /companies/{t}/notes`, `/footnote-facts`, `GET /filings/{id}/notes/{category}/diff`
  (scope on the existing diff); Footnotes tab. Caught: a re-run of the builder left stale ladder
  rows beside new gaps; AMD's "NOTE 1 –" capitals and JPM's bare "Note 12" cross-reference line
  broke segmentation (now handled). Dev DB re-parsed (216 re-split chunks lost embeddings),
  metrics rebuilt, summaries generated with gemini-3.8-flash (10 notes, 0 bullets dropped).
- 2026-10-24 — **Governance from proxy statements** (Phase 7g, ADR-0045): DEF 14A split into
  keyword sections at parse time (`sec-ingest-v4`); proxy fiscal year from its own PvP XBRL (else
  its table), not the filing date; Tier A `ecd` Pay Versus Performance facts and Tier B validated
  Summary Compensation Table rows (sum to total within $1) and CEO pay ratio (median × ratio within
  2%) in the new `exec_comp_facts` table (migration `f2a9c4e7b1d3`); Tier C `governance_v1` board
  facts, each a verbatim quote or null, with the independence ratio computed in code;
  `GET /companies/{t}/governance` and `/governance/history`; Governance tab; `make
  governance-backfill`. Caught on real proxies: JPMorgan's first matching table is a "pay
  decisions" table (now skipped when no row validates), rowspanned name cells, AMD's name-only
  rows, Apple tagging every officer as `ecd:PeoName`, a footnote pattern that ate 3-digit
  values, and the model pairing a current-board count with a nominee count (now dropped in code).
  Dev DB migrated, re-parsed, all 15 proxies built (0 rejected rows), Tier C extracted with
  gemini-3.8-flash.
- 2026-10-24 — **Peer sets v2 and valuation bands** (Phase 7h, ADR-0046): `peer_sets` /
  `peer_set_members` / `valuation_history` (migration `a6d2f8c3e5b1`); deterministic default peer
  rule with stored rationales; private user sets (≤12, unknown tickers named); point-in-time
  multiples (only facts filed by the date; TTM via prior FY + YTD − prior YTD, tags merged per
  period) for P/E, P/S, EV/Revenue, EV/EBITDA, EV/FCF, P/B, FCF and earnings yield, with
  `not_meaningful` for losses; five-year history and today persisted with source IDs; bands with
  `n` and minimum samples; positional labels checked by the advice guard (which now rejects
  cheap/expensive wording); `/peer-sets` CRUD, `/valuation-bands`, `/compare?peer_set_id=`;
  latest-close header multiples moved to TTM; Financials range bars and the Compare peer panel.
  Caught: NVIDIA's revenue switched tags between years (TTM now merges tags per period); Intel's
  FY2025 P/E read 872x on near-zero earnings (now not meaningful).
- 2026-10-24 — **Guidance vs actual** (Phase 7i, ADR-0047): `guidance_items` /
  `guidance_outcomes` (migration `b9e4d7a2c6f1`); the outlook of each EX-99.1 is read by one
  quote-checked model call (`guidance_extract_v1`, only the outlook sentences shown), and every
  item is verified in code (verbatim quote, exact numbers, fiscal-calendar period, keyword basis,
  "plus or minus" ranges computed in code); rejected items kept with reasons. Outcomes against
  the same period's GAAP rows in Decimal, non-GAAP never compared, initial guidance as headline
  with revisions; 8-quarter revenue track record (shown from 4); `guidance_miss` alert;
  `GET /companies/{t}/guidance` and `/guidance/track-record`; Guidance section on the Filings
  tab; `make guidance-backfill`. Dev: NVIDIA 46 verified items (revenue above the range in 7 of
  7 quarters), AMD 14 (5 of 6). Caught: an injected sentence produced a clean-looking quote (now
  the whole sentence is checked); NVIDIA's shared "respectively, plus or minus 50 basis points"
  and AMD's "plus or minus $300 million" needed their own tolerance rules.
- 2026-10-24 — **Earnings previews and briefs** (Phase 7j, ADR-0048):
  - **Calendar:** `earnings_events` and `earnings_briefs` (migration `f186174c8045`). Dates come
    from Item 2.02 8-Ks, a strictly matched company announcement, or an estimate shown as "~"
    that never notifies.
  - **Previews** (snapshot, guidance, track record, bands, flags, last reaction, watch items) and
    **briefs** (headline table, guidance outcomes, reaction with 5-day move and volume ratio,
    10-Q diff and topic shifts, new flags, a ≤4-sentence verified narrative) are assembled from
    stored data.
  - **Versions** are keyed by an inputs hash; `earnings_narrative_v1`; `brief_ready` alert;
    `generate_earnings_brief` / `generate_due_previews` tasks and a post-ingest hook;
    watched/seed cost guard and daily cap; calendar/briefs/regenerate endpoints; Earnings tab
    with version switcher and print layout.
  - **Dev:** a real gemini-3.8-flash NVIDIA Q2 FY2027 brief kept all 4 narrative sentences; the
    preview's 4 watch items were reworded with every number kept.
  - **Caught:** 10-Q risk factors were never recognised by the filing diff since 7c's
    part-qualified section keys (`"Part II, Item 1A"`), so 10-Q diffs and the `new_risk_factor`
    alert silently saw no risk factors (fixed in `filings/units.py`).
- 2026-10-25 — **Investment thesis tracker** (Phase 7k, ADR-0049):
  - **Data:** `theses`, `thesis_kpis`, `thesis_events` and `thesis_revisions` (migration
    `69ec64a10d9e`), owned through `user_id` and cascading on thesis and account deletion.
  - **KPIs:** a curated metric registry (stored, Q/TTM, scores, multiples, quarterly margins,
    revenue YoY, revenue vs guidance) with unit and sector checks at write. Decimal evaluation
    appends events only on data transitions; edits are baselines. One idempotent
    `refresh_company_theses` hook (after metrics, red-flag scans and briefs) links flags and
    briefs to timelines without changing KPI state.
  - **Alerts and AI:** a `thesis_trigger` alert scoped to the owner, once per (KPI, state,
    period end). "Draft from my notes" (`thesis_draft_v1`) suggests only, with registry metrics
    and no stance; a real model call ignored an injected instruction.
  - **API:** CRUD, events, revisions with word diffs, draft and metric registry; 404 isolation
    on every route.
  - **UI:** Thesis tab and Theses page, safe markdown, neutral/warning state chips, neutral
    sparklines, a History drawer.
  - **Tests:** a test that keeps thesis text out of logs and `/metrics`; a Playwright run
    against the dev stack on a throwaway company (`scripts/e2e_thesis.py`).
- 2026-10-29 — **Chat abstention and EPS** (found by the 7l eval): "diluted EPS" / "earnings per
  share" map to `eps_diluted` (it was falling to `net_income` or to document search); terse
  lookups ("NVIDIA revenue, latest annual figure?") are metric lookups; "gross profit" maps to
  `gross_profit` (a bank has none, so it declines); "inventory turnover" (not stored) declines
  instead of answering from loose passages; poems, live weather and five more injection phrasings
  are refused. Mock eval: correct abstention 14/24 → 24/24, false abstention 6/82 → 0/82,
  injection resistance 9/11 → 11/11 (the rules were written against these golden questions, so
  new phrasings can still slip through). Retrieval hit@5 now scores only questions that search
  documents (it counted stored-metric answers as misses); floors in `thresholds.json` raised.
  A bank's "gross profit" declines with the generic message, not "not applicable to banks".
- 2026-10-29 — **API**: a stored report that no longer validates is a logged 409 ("generate a new
  one") from both `/companies/{t}/research/latest` and `/research/{id}`, not a 500 (the Research
  tab already shows "a saved report could not be read" with a Generate button for it); the filings
  list pages by filing date (undated last), keeping the id cursor.
- 2026-10-29 — **Number check, SEC client, browser tests**: a claim's "$19,827 million" or "$19.8
  billion" now matches a table cell of 19,827 under an "(in millions)" header (thousands and
  billions too); `HardenedHttpClient` fails at once on a 4xx other than 429 (a bad key is no
  longer retried three times, and it no longer trips the breaker); older submission pages
  (`filings.files[]`) are stored in the object store keyed by name, range and count, so later
  ingests skip them and a page SEC re-cuts is fetched again; the company and portfolio browser
  tests serve AMD without prices (`withoutPrices` in `e2e/tests/helpers.ts`) and no longer
  depend on the dev data.
- 2026-10-29 — **API hardening**: responses over 1 KiB are gzipped (`GZipMiddleware`; SSE streams
  are left alone), which shrinks a company's ≈179 kB price history; the Lighthouse score was not
  re-measured. A request that waits 10 s for a `get_db` slot gets a logged 503 with
  `Retry-After: 1` instead of queueing in memory.
- 2026-10-29 — **Regenerate a report from the UI**: signed-in readers get "Regenerate" beside
  Export and Print; the report on screen stays until the new one is ready, then is replaced.
  The job flow is shared with "Generate" (`useReportJob`). Checked in the browser that the button
  shows only when signed in; a real regeneration run (about $0.05–0.10, needs the worker) was not
  clicked through.
- 2026-10-29 — **`llm_calls` records Gemini's reported tokens**: the client keeps each thread's
  last `usageMetadata` (thinking counted as output) and every caller (report sections, chat,
  guidance, briefs, governance, footnotes, theses, filing diff) logs it through
  `llm_calls.call_tokens`, falling back to the estimate for mocks. Cost is now priced from the
  input/output split with each rate; it was input tokens only at a blended rate (chat logged
  input + output as input), so recorded costs and budget spend will read higher, and closer to
  the bill. `estimate_blended_cost` was removed.
- 2026-10-29 — **Alert form shows the price threshold as a percent**: `PriceMoveParams.threshold`
  carries `"x-unit": "ratio"` in its schema; the generic form labels it "Move threshold (%)",
  takes 5 for 0.05 (bounds checked in percent), sends the fraction, and lists rules as
  "Move threshold: 5%".
- 2026-10-31 — **Parser v5 and Intel's sections** (ADR-0052): Intel's 10-Ks and 10-Qs (no "Item N"
  headings) are sectioned by their own headings ("Risk Factors" → Item 1A, "Management's
  Discussion and Analysis" → Item 7, …; the overview before "Our Business" is Item 1); FY2021–2023
  10-Ks still open with a "Filing text" section (their business heading is only a page footer).
  Two parser bugs fixed for every company: each table left literal "<p>"/"</p>" lines (about
  7,100 chunks held nothing else), and lines broke between inline elements, so inline-XBRL
  figures split sentences ("$ / 95.2 / billion") and notes chunks had a median of about 20
  characters. Lines now break only at block elements; footnote superscripts (also JPM's CSS ones)
  get a space so "$111,905" plus marker "2" is not "$111,9052"; proxy headings ignore a leading
  "I." or bullet. Text chunk IDs end in `_p{n}` so no older ID can name other text. Dev DB: backed
  up (`backups/assay-desk-20261101T014056Z.dump`), all 393 documents re-parsed (64,822 → 48,418
  chunks), stored references remapped by quote with `scripts/remap_chunk_ids.py` (71 guidance
  items, 18 red flags, 45 governance facts, 198 report sources, 90 brief citations, 24 pay facts;
  the unmatched ones cited junk chunks), red flags rescanned (no change), 10-K/10-Q/8-K chunks
  re-embedded (proxies and EX-99 releases stay unembedded, as before).
- 2026-11-01 — **Reports regenerated, JPM's 10-K sectioned, citation remap corrected**: the five
  latest research reports were regenerated with `gemini-3.8-flash` (now the `.env` model; ids
  23–27, 79–124 s each, $0.57 in all, every chunk citation resolving). JPM's Items 7 and 8 only
  point to the annual report bound in after Item 15, so that report's own headings now start Item 7
  (MD&A), 9A and 8 (statements and notes), and its glossary ends them (`_BOUND_REPORT`, ADR-0052);
  latest 10-K: Item 15 2,463 → 36 passages, MD&A 581, Item 8 1,019. The remap script first
  matched a reference without a quote to the first chunk holding the old opening words, which,
  with overlapping chunks, can be the previous chunk; it now keeps an unchanged ID, else picks
  the chunk sharing the most old text, and finds a table whose section changed by its table number.
  Every reference written by the 2026-10-31 remap was recomputed from the pre-v5 backup, and the
  new reports' sources from their own JSON; 287 JPM proxy pay facts whose tables moved into the
  v5 summary-compensation section resolve again. All 1,683 pay facts, 71 guidance items, 18 red
  flags, 45 governance facts, 29 brief citations and the new reports' 166 chunk sources resolve.
