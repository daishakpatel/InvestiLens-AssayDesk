# API

FastAPI, OpenAPI 3.1, versioned under `/api/v1` (spec §24). This is the human-readable map;
the machine contract is [`openapi.json`](openapi.json), generated from the app and consumed by
the frontend as a typed client (`frontend/src/services/api.d.ts`).

> **Contract-first.** Routes and Pydantic schemas are the source of truth. Regenerate the spec
> and client with `make openapi` after any change; a CI contract test fails if `openapi.json`
> drifts from the app. Additive changes only within `/v1`; breaking changes go to `/v2`
> (API-005) and require an ADR.

## Conventions (§24.1)

| ID | Convention |
|----|-----------|
| API-001 | Errors are RFC 7807 `application/problem+json` (`type`, `title`, `status`, `detail`, `instance`, `request_id`, optional `errors[]`), enforced by a shared handler. |
| API-002 | Cursor pagination (`?limit=&cursor=`); responses carry `next_cursor`. |
| API-003 | `Idempotency-Key` header honoured on `POST /research` and `POST /chat`. |
| API-004 | Server-Sent Events for job progress and chat streaming (`text/event-stream`). |
| API-005 | Additive-only within `/v1`; breaking changes → `/v2`. |
| API-006 | ISO-8601 UTC timestamps; monetary responses declare `unit`. |
| API-007 | `X-Request-ID` is accepted, echoed on the response, and included in error bodies. |
| API-008 | Data endpoints return freshness (`as_of`, `source`, `freshness_status`). |

> **Phase 4a status:** endpoints are wired to their real backing stores (companies/financials →
> Phase 1, filings/news → 1a/1e, research/sources → Phase 3, chat → 3c). Features whose data is
> not yet ingested return a clear empty/`404`/`501` (insiders/ownership P2, filing & research diff
> Phase 6, export Phase 4d) rather than faked data. Auth is implemented in Phase 4b (see below).

### Cross-cutting (Phase 4a)

- **Pagination (API-002, ADR-0016):** list endpoints accept `?limit=` (default 50, max 200) and an
  opaque `?cursor=`. `GET /companies/{ticker}/news` carries `next_cursor` in the body; the other
  list endpoints (bare arrays, whose contract shape is frozen) return it in an `X-Next-Cursor`
  response header. A malformed cursor is a 422 problem.
- **Idempotency (API-003, ADR-0017):** `POST /research` single-flights — a repeat with the same
  `Idempotency-Key`, or any in-flight job for the same company, attaches to the existing job
  instead of starting a second one. The job is enqueued (`queued`); the Phase 5d worker runs it.
- **Freshness (API-008):** data endpoints read `data_freshness` for their source (`sec`/`price`/
  `news`) and report `as_of`/`source`/`freshness_status` (`stale` when never ingested).
- **Rate limiting (ADR-0018/0022):** `RateLimitMiddleware` is disabled by default
  (`rate_limit_enabled`); when on, a general `429` tier (default 100/min, spec §25.4) applies to
  every route and a tighter AI tier (default 20/hour) applies to `/chat*` and `POST /research`,
  keyed per-user when authenticated else per-IP. Backend is `"memory"` (default, in-process) or
  `"redis"` (real cross-worker limiting via `REDIS_URL`). Responses carry `Retry-After`.
- **Request size (NFR-ME-009, Phase 7l):** every `POST`/`PUT`/`PATCH` body over
  `MAX_REQUEST_BYTES` (default 256 KiB) is refused with `413` before it is read; a body sent
  without a `Content-Length` (chunked) gets `411`. Problem+json, like every error.
- **Demo mode (NFR-ME-007/009, ADR-0050):** with `DEMO_MODE=true`, `POST /auth/register` answers
  `403`, `POST /auth/demo` signs the caller into the shared sandbox account, rate limiting is on
  whatever `rate_limit_enabled` says and is keyed by client IP (everyone shares one account), and
  the LLM and embedder replay recorded/offline output only.
- **AI budget (NFR-008, ADR-0022):** `POST /chat` (qualitative answers only) and `POST /research`
  return `402` (`application/problem+json`) once a user's monthly AI spend reaches their
  `ai_budget_month_usd` cap (set at registration; `default_ai_budget_month_usd` in config).
- **Auth (ADR-0005/0006):** chat, watchlists, and alerts require a user via a Bearer JWT; admin
  endpoints additionally require `role=admin`.
- **Observability (ADR-0021):** `GET /metrics` (root-level, not under `/api/v1` — it's a scrape
  endpoint, not a business API) exposes Prometheus metrics; see `docs/observability.md`.

## Endpoints (§24.2)

### Health & meta
| Method | Path | Notes |
|--------|------|-------|
| GET | `/health` | Liveness |
| GET | `/ready` | Readiness (db, redis, queue) |
| GET | `/meta/data-freshness/{ticker}` | Per-source freshness |
| GET | `/meta/demo` | `{enabled, data_as_of}`: whether this is the public demo, and the latest fixture filing date for its banner (NFR-ME-007) |

### Company
| Method | Path | Notes |
|--------|------|-------|
| GET | `/companies/search?q=` | Fuzzy search (FR-001). **Declared before `/{ticker}`.** |
| GET | `/companies/{ticker}` | Company profile + freshness |

### Financials & prices
| Method | Path | Notes |
|--------|------|-------|
| GET | `/companies/{ticker}/financials?metrics=&period_type=FY\|Q\|TTM&limit=&from=&to=` | Metric series. `period_type` is validated (422 otherwise). `limit` (1–20) keeps the latest N periods per metric; Q and TTM default to 8, FY returns every year unless `limit` is given (FR-QT-011). Each point carries its `source_id` (resolve via `/sources/{id}`) and, for a derived row, its `formula` text, e.g. `Q3 = 9M YTD - 6M YTD` or `TTM = Q4-FY2025 + Q1-FY2026 + Q2-FY2026 + Q3-FY2026` (Phase 7b, ADR-0040). Per-share (`USD/shares`) and share-count (`shares`) values are split-adjusted to today's share basis: a figure filed before a later stock split is divided (a count multiplied) by that split's ratio, and its `formula` says so, e.g. `Split-adjusted: as filed 11.93, divided by 10 for later stock splits (10-for-1 on 2024-06-10)`; `source_id` still cites the as-filed figure (DR-026, ADR-0051). Each series also carries `applicable`: `false` when the metric is N/A for the company's sector (`sector_applicability.yaml`, DR-042), e.g. a bank's operating income, capex or free cash flow, so it reads as not applicable rather than unreported. |
| GET | `/companies/{ticker}/metrics/coverage` | Quarterly / TTM coverage (Phase 7b, FR-QT-010): `quarters` (the stored window, up to 12, oldest first) and, per metric, `present` periods (`Q2-FY2026`, `TTM-Q2-FY2026`), `missing` periods with a `reason` (`missing_ytd_2q`, `missing_fy`, `missing_q2`, `missing_balance`, `not_additive`, `period_not_reported`, `incomplete_window`, or a stored NULL's reason code such as `NEGATIVE_BASE`), and `applicable` (false = not applicable to the sector, DR-042: render N/A, not missing). |
| GET | `/companies/{ticker}/metrics/{metric_name}/lineage?period=` | Derivation lineage (CIT-003) |
| GET | `/companies/{ticker}/earnings-quality?years=` | Earnings-quality dashboard (Phase 7d, FR-EQ-009/010, ADR-0042). `years` is 1–10, default 5 (422 otherwise). Returns `fiscal_years` (oldest first) and `measures[]` in display order (`accruals_ratio`, `accruals_ratio_bs`, `fcf_conversion`, `dso`, `dio`, `dpo`, `cash_conversion_cycle`, `dilution`, `beneish_m`, `altman_z`, `piotroski_f`). Each measure carries `label`, `unit`, a static `explanation` (never LLM text), a `caveat` (the screening-heuristic text, on the three scores only), `points[]` per fiscal year and `latest_ttm` (accruals ratio and FCF conversion only). Each point has `value` (null unless `status` is `ok`), `status` (`ok`, `insufficient_data` or `not_applicable`), `band`, `reason` (code), `reason_text` (e.g. "Not computed for banks: …"), `missing` (input names), `source_id`, `formula_id`, `inputs[]` (name, value, source ID; each resolves via `/sources/{id}`) and `details` (Beneish `indices`/`threshold`, Altman `model_variant`/`variant_basis`, Piotroski `criteria`/`computable`). `peer_percentile` is the share of the peer set (`suggest_peers`, target included) at or below the target's latest fiscal-year value. It is null when fewer than 3 peers report the measure (`peer_count`), with a `notes` entry saying why. |
| GET | `/companies/{ticker}/valuation` | Valuation metrics: the multiples at the latest close where stored, else the latest fiscal year end (ADR-0036) |
| GET | `/companies/{ticker}/prices?start=&end=&interval=` | OHLCV |
| GET | `/companies/{ticker}/insiders` | Form 4 (P2) |
| GET | `/companies/{ticker}/ownership` | 13F (P2) |

### Filings
| Method | Path | Notes |
|--------|------|-------|
| GET | `/companies/{ticker}/filings?type=&limit=&cursor=` | Filing list. Each item carries `document_type`: `filing`, or `late_filing_notice` for an NT 10-K / NT 10-Q (Phase 7c). Forms: 10-K, 10-Q, 8-K, 8-K/A, DEF 14A, DEFA14A, NT 10-K, NT 10-Q |
| GET | `/filings/{filing_id}` | Filing detail (incl. `document_type`; 8-K `items` are full dotted codes, e.g. `5.02`) |
| GET | `/filings/{filing_id}/sections` | Section outline |
| GET | `/filings/{filing_id}/diff?against=&kind=` | Filing diff (Phase 6b, §37.4): what is `unchanged / reworded / new / removed` versus an older filing of the same type. `against` defaults to the prior filing of that type (last year's 10-K, last quarter's 10-Q). `kind=risk_factor\|section` filters. Every item carries `old`/`new` passages with backend-issued `source_ids` (resolve via `/sources/{id}`), `decided_by`, and a word-level `word_diff` when reworded. Public and deterministic: no LLM. 404 = no earlier parsed filing; 422 = different company/type or an unparsed filing. |
| GET | `/companies/{ticker}/earnings` | Earnings-day price reaction (§37.3): adjusted-close move around each Item 2.02 8-K. `move` is a ratio; `null` + `reason` when prices are missing. No expectations data (ADR-0027). Phase 7j adds `move_at_open` (first session's open, adjusted, vs the prior close), `move_5d` (fifth session's close vs the prior close) and `volume_ratio` (first session's volume over the prior 20-session average; null without volume data). |
| GET | `/companies/{ticker}/management-language?filing_type=10-K&limit=8` | Management language tracking (§37.5): per-filing topic mentions per 1,000 MD&A words (`topic_taxonomy_v1`), a lexicon hedging index, first-appearance/disappearance flags, and the counted statements with `source_id`s. Lexicon counts, not AI. |

### Footnotes (Phase 7f, FR-FN-010/011, ADR-0044)
| Method | Path | Notes |
|--------|------|-------|
| GET | `/companies/{ticker}/notes?filing_id=` | The notes to the financial statements of a 10-K or 10-Q (default: the latest parsed 10-K). Each note: `note_id` (`note-7`), `number`, `title`, `category` (`note_taxonomy_v1`), `also_matches` (other categories the title matched), `paragraph_ids` (its chunks; resolve via `/sources/{id}`), and for revenue recognition, commitments and contingencies, and segments a `summary`: `status` `summarised` (verified `bullets`: `text`, `quote`, `paragraph_id`), `abstained` (show "Not summarised") or `pending` (not generated yet; this endpoint never calls a model). `low_confidence` is true when a 10-K yielded fewer than five notes or was not segmented. 404 = no parsed 10-K, or a filing of another company; 422 = not a 10-K/10-Q. |
| GET | `/companies/{ticker}/footnote-facts` | The latest fiscal year's XBRL footnote facts by category (`debt`, `leases`, `income_taxes`, `commitments_contingencies`, `stock_comp`). Each fact: `metric`, `label` (lease-adjusted figures are always named so), `value` (decimal string; `unit` `USD`, `ratio` or `years`), `period`, `source_id` (`xbrl:` or `derived:`), `is_derived`, `formula`, `from_inline_xbrl`. `gaps` lists what the filing did not tag (`not_tagged`, `filing_unavailable`, `MISSING_INPUT`). `fiscal_year: null`, `categories: []` before the first build. |
| GET | `/filings/{filing_id}/notes/{category}/diff?against=` | The filing diff scoped to one note category in both filings (same response as `/filings/{id}/diff`, same baseline rules). 404 = unknown category or no earlier filing; 422 = the note is missing from one filing. |

### Governance (Phase 7g, FR-GV-011, ADR-0045)
| Method | Path | Notes |
|--------|------|-------|
| GET | `/companies/{ticker}/governance?fiscal_year=` | One proxy statement (default the latest; `fiscal_year` is the year it covers, from its content, not its filing date). `filing`, `fiscal_year_source`, `available_years`, `proxies_ingested`, `coverage` (proxy section → found). `facts` (Tier C, AI): `status` `extracted`, `pending` or `no_sections`; `fields[]` each `{name, label, value, quote, paragraph_id}` with `value: null` = not stated; `independence_ratio` computed in code. `compensation` (Tier B): Summary Compensation Table `rows[]` that add up (`person`, `role` peo/pfo/neo, `fiscal_year`, `components`, `total`, `source_id`), `rejected_rows`. `pay_vs_performance` (Tier A): `points[]` `{fiscal_year, values, source_ids}`, `company_selected_measure`. `pay_ratio`: `ratio`, `median_pay`, `ceo_total`, `source_id`. A missing part has null values and a `reason`. With no processed proxy: `filing: null` and a `reason`. 404 = no proxy for that fiscal year. Never calls a model. |
| GET | `/companies/{ticker}/governance/history` | `points[]` by fiscal year: `ceo_total_pay` (the Summary Compensation Table total, else PvP `PeoTotalCompAmt`, from the newest proxy reporting the year) with its source, and that proxy's `independence_ratio`. |

### News
| Method | Path | Notes |
|--------|------|-------|
| GET | `/companies/{ticker}/news?category=&from=&to=&source=&min_relevance=` | Headlines, newest published first (LGL-004). Without `min_relevance`, rows below `NEWS_RELEVANCE_THRESHOLD` (0.3) are left out; pass `min_relevance=0` for all |

### Research (async job pattern, §25.1)
| Method | Path | Notes |
|--------|------|-------|
| POST | `/research` | 202 + `job_id`; `Idempotency-Key` (API-003) |
| GET | `/research/jobs/{job_id}` | Poll job state |
| GET | `/research/jobs/{job_id}/events` | SSE progress (API-004) |
| GET | `/research/{research_id}` | Completed report (409 if it no longer matches the current report schema) |
| GET | `/companies/{ticker}/research/latest` | Latest report (404 if none; 409 if it no longer matches the current schema) |
| GET | `/research/{research_id}/diff?against=` | Report diff (P2, 501) |
| GET | `/research/{research_id}/export?format=pdf\|md` | Export (Phase 4d, 501) |

### Sources (citation modal)
| Method | Path | Notes |
|--------|------|-------|
| GET | `/sources/{source_id}` | Source record + span + deep link (CIT-006). A `redflag:<TICKER>:<rule_key>:<accession>` ID resolves to the flag's quoted filing text as a `text_chunk` (section path `[form, label]`), with the filing as `deep_link` (Phase 7e). |

### Guidance vs actual (Phase 7i, FR-GD-014, ADR-0047)
| Method | Path | Notes |
|--------|------|-------|
| GET | `/companies/{ticker}/guidance?fiscal_year=` | Verified company guidance (from the company's own EX-99.1 releases; never consensus estimates), newest period first; `fiscal_year` (1990–2100) filters, omitted = all. `scope` states the company-guidance-only rule; `fiscal_years` lists years with guidance. Each item is the **initial** guidance for a `(period, metric, basis)`: `fiscal_year`, `fiscal_period` (Q1–Q4/FY), `period_label`, `metric` (`guidance_metric_v1`), `metric_label`, `basis` (`gaap`/`non_gaap`/`unspecified`), `unit` (`usd`/`ratio`/`percent_of_revenue`), `low`+`high` or `point`, `midpoint`, `derived_range` (range computed from "plus or minus"), `quote`, `source_id` (the release paragraph; opens the citation drawer), `issued_at`, `release {document_id, title, url}`, `outcome` (`result` above/within/below_range, `not_comparable` or `actual_unavailable`; `actual_value`, `actual_source_ids` (the metric rows' IDs), deltas vs low/high/mid, `delta_vs_mid_pct`, `reason_code`, `explanation`), and `revisions[]` (later releases' guidance for the same key, same shape without period fields). Decimals are strings. Never calls a model. |
| GET | `/companies/{ticker}/guidance/track-record` | Revenue against its initial guided range over the latest 8 guided quarters: `n_quarters`, `n_comparable`, `n_above`, `n_within`, `n_below`, `avg_delta_vs_mid`, `avg_delta_vs_mid_pct`, `sufficient` (needs 4 comparable), `label` (positional: "Revenue came in above the guided range in 6 of 8 quarters, …", or "Insufficient history (n=3)"), `quarters[]` newest first (`period_label`, `low`, `high`, `actual_value`, `result`, `delta_vs_mid_pct`, `source_id`). |
| GET | `/companies/{ticker}/earnings/calendar` | Earnings dates per fiscal quarter, newest first (Phase 7j, FR-EB-001/002, ADR-0048). Refreshes the calendar from stored filings first (idempotent upsert). Each event: `fiscal_year`, `fiscal_period` (Q1–Q4), `period_label`, `report_date` (null when `status` is `unknown`), `report_time` (`bmo`/`amc`/`unknown`), `source` (`sec_8k` = an Item 2.02 8-K was filed, `source_id` `filing:<id>`; `announcement` = the company's own sentence in a later 8-K or release, kept in `quote`, `source_id` its paragraph; `estimated` = last year's date plus 52 weeks), `status` (`reported`/`scheduled`/`unknown`), `estimated` (show with "~"; an estimate never triggers a preview or notification). `upcoming` is the newest unreported quarter or null. |
| GET | `/companies/{ticker}/earnings/briefs?kind=&fiscal_year=&fiscal_period=` | Stored pre-earnings previews and post-earnings briefs, every version (FR-EB-013/015): latest quarter first, then highest version, at most 20. `kind` is `preview`/`brief`, `fiscal_period` `Q1`–`Q4` (422 otherwise). Each item: `id`, `kind`, `fiscal_year`, `fiscal_period`, `period_label`, `version`, `inputs_label` (what changed from the previous version, e.g. "after 10-Q filed"), `created_at`, and `preview` or `brief`. Both carry `header` (date, its source, `date_estimated`) and `missing[]` (`section`, `reason`: a section whose input is missing, shown as "Not available: {reason}"). Preview: `last_quarter_snapshot` (revenue, gross/operating margin, GAAP EPS, operating cash flow with prior-quarter and prior-year changes; growth as a ratio, margins in ratio points; every value with `source_ids`), `guidance_for_period[]` (7i rows), `track_record` (only when sufficient), `valuation_snapshot` (P/E, EV/EBITDA with 7h positional labels), `open_flags[]` (red flags of the last 90 days), `last_reaction`, `watch_items[]` (`kind`, `text`, `source_ids`, `reworded`: built in code, optionally reworded by the model with every number kept). Brief: `headline_table`, `versus_guidance[]` (rows with outcomes), `reaction`, `what_changed` (this 10-Q against the previous one: new/removed/reworded risk counts, `top_changes[]` with old/new quotes, `topic_shifts[]`), `new_flags[]` (since the previous report), `narrative` (`sentences[]` each with `source_ids`, at most 4 and 120 words, all verified; `dropped`, `prompt_version` `earnings_narrative_v1`, `model`). `scope` states that no analyst estimates are used. Never calls a model. |
| POST | `/companies/{ticker}/earnings/briefs` | Body `{kind}`, `preview` or `brief`. Signed in, within the AI budget, and at most `EARNINGS_BRIEF_REGEN_PER_HOUR` (default 5) calls per user per hour (429 with the wait in `detail`). Makes the current quarter's preview (next unreported quarter) or brief (latest reported quarter). Returns `{created, item}`: `created` false means the inputs were unchanged and the stored version came back without a model call. 409 when the company is not on any watchlist or in the seed set, or there is no quarter yet; 429 when today's `EARNINGS_BRIEF_DAILY_CAP` is reached (logged). |

### Red flags (Phase 7e, FR-RF-009/010, ADR-0043)
| Method | Path | Notes |
|--------|------|-------|
| GET | `/companies/{ticker}/red-flags?since=&severity=&limit=` | Reported filing events, newest `event_date` first, current taxonomy version only. `since` (ISO date) keeps events filed on or after it; `severity` is a **minimum** (`info` < `warning` < `critical`, 422 otherwise); `limit` 1–200, default 50. Each item: `source_id`, `ticker`, `rule_key`, `label`, `severity`, `explanation`, `rule_source` (the SEC form/standard), `event_date` (filing date), `snippet` (the filing's words), `filing {id, filing_type, accession_number, filing_date, url}`, `details` (item code, roles, …). The response always carries `last_scanned_at` (null until the first scan) and `taxonomy_version`, so an empty `items` means "scanned through that date, nothing reported". |
| GET | `/red-flags?watchlist=true&since=&severity=&limit=` | The same, across every company on the caller's watchlists. Auth required (401); `watchlist=false` is 422 (only the watchlist feed exists). `last_scanned_at` is the oldest of those companies' last scans. |

### Chat
| Method | Path | Notes |
|--------|------|-------|
| POST | `/chat` | Non-streaming answer + citations. `question` is 1 to 2,000 characters after trimming, else 422 (SEC-002) |
| POST | `/chat/stream` | SSE token stream (API-004) |
| GET | `/chat/sessions/{id}` | Session history |
| POST | `/chat/messages/{id}/feedback` | Thumbs up/down |

Chat requires an authenticated user (ADR-0006: no anonymous AI). Phase 3c resolves the user from an
`X-User-Id` header as an interim seam; Phase 4b replaces it with JWT verification without changing
call sites. The answer is retrieval-routed (structured metrics vs documents), citation-verified
(Phase 3a), and returns `evidence_label`, `abstained`/`refused`, `tool_trace`, `suggested_questions`,
and `message_id` (for feedback).

### Auth (Phase 4b — implemented, ADR-0005/0006)
| Method | Path | Notes |
|--------|------|-------|
| POST | `/auth/register` | Argon2id hash, password policy (AUTH-001); 201 → profile. `403` in demo mode |
| POST | `/auth/demo` | Demo mode only (else `404`): tokens for the shared sandbox account, created on first use and deleted nightly with its data (NFR-ME-007, ADR-0050) |
| POST | `/auth/login` | Access JWT in body + rotating refresh token in httpOnly/Secure/SameSite cookie; rate-limited (AUTH-006) |
| POST | `/auth/refresh` | Rotates the refresh cookie; reuse of a revoked token revokes the family (AUTH-002). A token rotated in the last 10 s whose successor is unused is a second tab, and gets its own token (ADR-0030) |
| POST | `/auth/logout` | Revokes the token family and clears the cookie |
| GET | `/auth/me` | Current profile (Bearer access token) |
| DELETE | `/auth/me` | Soft-deletes the account + revokes sessions; hard-delete via a retention job (LGL-007) |
| POST | `/auth/verify-email/request` · `/auth/verify-email/confirm` | Single-use email-verification token (AUTH-003) |
| POST | `/auth/password-reset/request` · `/auth/password-reset/confirm` | Single-use reset token; neutral ack (no user enumeration) |

Access tokens are HS256 JWTs (15 min, claims `sub`/`exp`/`iat`/`jti`) sent as `Authorization:
Bearer`; the frontend holds them in memory. Protected routes resolve the user from the verified
token (`get_current_user`), replacing Phase 3c/4a's interim `X-User-Id` seam with no call-site
change. Object-level authorization (SEC-007/AUTH-004) scopes every user-owned resource to the
caller. Per ADR-0006, anonymous users may browse company data and view already-generated reports,
but **may not** generate reports, use chat, or touch user-owned data (anonymous AI quota 0).

### Watchlist, alerts & feedback
| Method | Path |
|--------|------|
| GET / POST | `/watchlists`, `/watchlists/{id}` (DELETE) |
| GET | `/alerts/event-types` |
| GET / POST | `/alerts` |
| PATCH / DELETE | `/alerts/{id}` |
| GET | `/notifications`, POST `/notifications/{id}/read` |
| POST | `/feedback/report` |

### Investment theses (Phase 7k, FR-TH-013, ADR-0049)
Signed-in only; every route is scoped to the caller, so another user's thesis, KPI, event or
revision answers **404** (SEC-TH-001). Thresholds and values are decimal strings in the metric's
unit (ratios as decimals). Thesis text is private and never logged.

| Method | Path | Notes |
|--------|------|-------|
| GET | `/theses?ticker=` | The caller's theses, most recently updated first: `id`, `ticker`, `title`, `stance` (`long`/`short`/`watch`/`avoid`, the user's tag), `status` (`active`/`closed`), dates, `kpi_states {met, broken, no_data}`. |
| POST | `/theses` | `{ticker, title ≤200, body_md ≤20,000, stance}` → 201 `ThesisDetail` (summary + `body_md` + `kpis[]`); revision 1 is stored. 404 unknown ticker; 409 at 20 active theses. |
| GET / PATCH / DELETE | `/theses/{id}` | PATCH any of `title`, `body_md`, `stance`, `status`. A new title or body adds a revision; a status change adds a `status_changed` event. DELETE (204) hard-deletes the thesis with its KPIs, events and revisions (FR-TH-014). |
| POST | `/theses/{id}/kpis` | `{metric, period_type, comparator (gte/lte/gt/lt), threshold, label ≤120, rationale? ≤1,000}` → 201 `Kpi`, evaluated at once as a baseline (no event). Problems are 422 with `errors[].field`: `Unknown metric`, `Unsupported period`, `Metric not applicable` (sector), `Threshold looks like a unit mistake` (e.g. 62 for a ratio: "enter 0.25 for 25%"). 409 at 10 KPIs. |
| PATCH / DELETE | `/theses/{id}/kpis/{kpi_id}` | Changing metric, period, comparator or threshold re-validates and sets a new baseline (no event). |
| GET | `/theses/{id}/events?limit=` | The timeline, newest first (`limit` 1–200, default 50): `kind`, `occurred_at`, `kpi_id`, `ref_type` (`metric`/`red_flag`/`earnings_brief`), `ref_id` (a source ID to open), `note`, `payload` (KPI value, period, `period_end`, source IDs, threshold). |
| POST | `/theses/{id}/events` | `{note ≤2,000}`: a `user_note` on the timeline. |
| GET | `/theses/{id}/revisions` | Every revision, newest first, with `word_diff` (ops `equal`/`insert`/`delete`) against the previous one; null for the first. |
| POST | `/theses/draft` | `{ticker, notes ≤5,000}` → suggested `title` and up to 5 `kpis` (registry metrics only, each re-validated), `dropped`, `prompt_version` `thesis_draft_v1`, `notice`. Nothing is saved; no stance is ever suggested. Within the AI budget and at most `THESIS_DRAFT_PER_HOUR` (default 10) per user per hour (429). |
| GET | `/thesis/metric-registry?ticker=` | Trackable metrics: `key`, `label`, `group`, `unit` (`usd`/`usd_per_share`/`ratio`/`multiple`/`score`/`days`), `period_types`, `description`, `applicable` for the ticker's sector (null without one). Public. |

**Alert rules (Phase 7a, ADR-0039).** `GET /alerts/event-types` returns the event-type registry:
`{key, label, description, params_schema, default_enabled}` per type, where `params_schema` is
the JSON Schema of that type's params, so a client renders the rule form with no hard-coded list.
A number property marked `"x-unit": "ratio"` is stored as a fraction and shown and typed as a
percent by the form (0.05 is entered as 5).
Types today: `new_filing` (`form_types` ⊆ `10-K`/`10-Q`/`8-K`), `price_move` (`threshold`, a
fraction: 0.05 = 5%), `new_risk_factor` (no params; fires when the newest parsed 10-K/10-Q adds a
risk factor its predecessor did not, Phase 6b), `news_category` (optional `category`),
`red_flag` (Phase 7e) and `guidance_miss` (Phase 7i: `metrics` default `revenue`, `eps_gaap`;
`results` default `below_range`, may add `above_range`; event key `guidance_outcome:{id}`, fires
once per initial-guidance outcome, not for history older than 200 days), and `brief_ready`
(Phase 7j, no params; event key `brief_ready:{ticker}:{fiscal_year}:{fiscal_period}:{kind}`, so a
quarter's preview or brief notifies once whatever its version; previews of estimated dates never
notify; `source_ids` is `earnings_brief:{id}`, which the UI opens on the Earnings tab), and
`thesis_trigger` (Phase 7k: `notify_on` default `broken`, may add `met`; only the alert owner's
own theses; key `thesis_kpi:{kpi_id}:{state}:{period_end}`, so a flapping value notifies once per
state per period; the body quotes the user's label and threshold and the cited value only).

`POST /alerts` body: `{event_type, ticker?, params?, channels?, enabled?}`. `ticker` omitted or
null = every company on the caller's watchlist, evaluated at dispatch time. `params` are
validated against the type and returned with defaults filled in; an unknown `event_type` or bad
params returns the RFC 7807 envelope with field-level `errors` (e.g. `body.params.threshold`).
`channels` ⊆ `in_app`, `email` (default `["in_app"]`). At most `ALERT_MAX_PER_USER` (50) alerts
per user; past that, 422. `PATCH /alerts/{id}` takes any of `params`, `channels`, `enabled`.
Alerts are user-scoped: another user's id answers 404, never 403. Each `Alert` carries
`supported: false` when its stored type is no longer in the registry (shown, never dropped).

A new alert fires only on events that occur **after** it was created (per-alert watermark), and
each event is delivered once per alert. More than 20 events for one alert in one run arrive as
a single digest notification.

**Notifications** add `ticker`, `event_type`, `severity` (`info|warning|critical`) and
`source_ids`: backend-issued evidence ids the client links to — `filing:<filing id>`,
`news:<news id>`, `price:<TICKER>:<from>:<to>`, or filing chunk ids. `price:` ids resolve through
`GET /sources/{id}` to a `price_bar` source (ADR-0054); `filing:` ids are not (yet) resolvable.

### Admin (internal)
| Method | Path |
|--------|------|
| GET | `/admin/ingestion-runs` |
| GET | `/admin/eval-runs` |
| POST | `/admin/companies/{ticker}/refresh` |

### Comparison & portfolio (Phase 6a, §37.1/§37.2)
| Method | Path | Notes |
|--------|------|-------|
| GET | `/compare?tickers=NVDA,AMD,INTC&metrics=…&period=…` | Deterministic side-by-side: calendarized (aligned fiscal periods, DR-021), sector-normalized percentiles. Public. 2–6 tickers; `metrics`/`period` optional (default set; latest common calendar year). Or `peer_set_id=` instead of `tickers` (Phase 7h): the set's company and all its members; a private set of another user is 404, an empty set 422. |
| GET | `/compare/peers/{ticker}` | Peer-set suggestions by SIC/industry + market-cap band. Public. |
| POST | `/compare/commentary` | AI commentary on the differences — cited + verified by the Phase 3a pipeline, non-advisory (LGL-006). Requires auth + AI budget (like research/chat). |
| POST | `/portfolio/analyze` | Analyze a hypothetical portfolio (ticker + weight). Deterministic weighted metrics, HHI concentration, sector exposure, price-based volatility/correlation, and aggregated **cited** risk themes. Public (no LLM generation). Returns `holdings_data` so the client recomputes what-if weight changes with no round-trip. |

### Peer sets and valuation bands (Phase 7h, FR-PS-012, ADR-0046)
| Method | Path | Notes |
|--------|------|-------|
| GET | `/companies/{ticker}/peer-sets` | The system default set (rule: same SIC, 0.25×–4× market cap, same currency, ≤8, nearest cap first) and, with a bearer token, the caller's own sets. Each: `id`, `name`, `kind`, `members[]` (`ticker`, `name`, `rationale`), `insufficient` (<3 members), `reason`. |
| POST | `/companies/{ticker}/peer-sets` | Auth. `{name, tickers}` (1–12). 422 names unknown tickers ("Not in Assay Desk yet: … Add this company to your watchlist first …"), the company itself, or too many. 201 with the set. |
| PATCH | `/companies/{ticker}/peer-sets/{id}` | Auth, owner only (else 404; the system default is not editable). `name` and/or `tickers`. |
| DELETE | `/companies/{ticker}/peer-sets/{id}` | Auth, owner only. 204. |
| GET | `/companies/{ticker}/valuation-bands?peer_set_id=` | Per multiple: `unit` (`x` or `ratio`), today's `status`/`value`/`reason`; `own` (`n`, `min`, `median`, `max`, `percentile`, positional `label`, `reason`: a band needs 3 fiscal year ends, a percentile 5 values); `peers` (`n`, `median`, `percentile`, `premium_to_peer_median` as a decimal, `label`, `reason`, `excluded[]` with reasons; needs 3 peers with a value); `history[]` (each fiscal year end and today, point in time). `peer_set_id` defaults to the system default; a private set of another user is 404. Never judges value: labels are positions only. |

Every comparison/portfolio response carries a `disclaimer`: analysis only, never a buy/sell/hold
recommendation. The `compare_companies(tickers, metrics, period)` read-only tool (§16.7) exposes the
same deterministic comparison to the agent layer. Calendarization and the company-scoped derived
`source_id` scheme are ADR-0026.

## Regenerating the contract

```bash
make openapi   # writes docs/openapi.json and frontend/src/services/api.d.ts
```
