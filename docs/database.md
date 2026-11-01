# Database

PostgreSQL 16 + [pgvector](https://github.com/pgvector/pgvector) ([ADR-0002](decisions/0002-pgvector.md)).
Models live in `backend/app/models/`; migrations in `infrastructure/migrations/` (Alembic).
This doc is the map — the models are the source of truth, and any schema change updates this
file in the same PR (DB-001).

## Conventions (spec §23.4, docs/conventions.md)

- **Money** is `NUMERIC(24,4)` (`Money` type alias). **Ratios / derived values** are
  `NUMERIC(28,8)` (`Quantity`), which also holds share counts that exceed integer ranges.
  **No monetary column is ever float** (DB-003, checked by a migration test).
- **Timestamps** are `TIMESTAMPTZ`, stored in UTC. **Fiscal-period fields** are separate
  columns, never derived from calendar dates.
- **Primary keys** are `BigInteger` identity (headroom for NFR-007's 10× growth).
- **Constraint/index names** follow a fixed naming convention (see `app/models/base.py`) so
  migrations stay deterministic and reviewable.

## Tables by domain (46 tables)

```text
Reference        companies ──< company_identifiers
                 companies ──< fiscal_calendars

Documents        companies ──< documents ──< filings (filings.amends_filing_id ─┐ self-ref)
                 documents ──< document_chunks                                   │
                 documents ──< news (news.document_id, nullable)                 │
                 filings ──< red_flags >── companies, documents                    │
                 documents ──< exec_comp_facts >── companies (proxy pay figures)    │
                 documents ──< guidance_items ──1 guidance_outcomes (company guidance)

Comparison       companies ──< peer_sets ──< peer_set_members >── companies (peers)
                 users ──< peer_sets (owner; NULL for the system default)
                 companies ──< valuation_history (multiples by date, point in time)

Financials       companies ──< financial_facts        (raw XBRL, point-in-time)
                 companies ──< financial_metrics       (computed, versioned)
                 companies ──< metric_gaps             (quarters / TTM that could not be computed)
                 companies ──< price_history
                 companies ──< corporate_actions

Ownership        companies ──< insider_transactions
                 companies ──< institutional_holdings

AI output        companies ──< research_reports ──< research_sources
                 companies ──< earnings_events (one per fiscal quarter: report date + its source)
                 companies ──< earnings_briefs (preview / brief versions by inputs hash)
                 research_reports ──< claim_verifications >── chat_messages
                 research_reports (supersedes_report_id ─┐ self-ref)

Users            users ──< refresh_tokens (replaced_by_id ─┐ self-ref, rotation chain)
                 users ──< auth_tokens (single-use email-verify / password-reset)
                 users ──< watchlists ──< watchlist_items >── companies
                 users ──< alerts >── companies   (company_id NULL = whole watchlist)
                 users ──< notifications >── alerts, companies
                 users ──< chat_sessions ──< chat_messages
                 users ──< theses >── companies
                 theses ──< thesis_kpis, thesis_events, thesis_revisions (private; cascade)
                 users ──< report_feedback >── research_reports

Operations       ingestion_runs, ingestion_dead_letters, jobs, llm_calls,
                 retrieval_logs, data_quality_issues, eval_runs ──< eval_results,
                 data_freshness
```

`──<` = one-to-many (FK on the right table). `>──` = many-to-one reference.

### Why `financial_facts` and `financial_metrics` are separate (spec §11.4, §13.4)

`financial_facts` stores raw XBRL exactly as filed — immutable, point-in-time, with
`is_amended` for restatements. `financial_metrics` stores canonical computed values with
lineage (`is_derived`, `formula_id`, `source_id`, `as_reported_accession`, `is_latest`). Keeping
them apart lets us recompute metrics, track restatements, and cite a derived number back to the
exact facts it came from without mutating source data.

A derived metric's `source_id` is **company-scoped** — `derived:<TICKER>:<formula_id>:FY<year>`
(Phase 6a, ADR-0026). It was previously `derived:<formula_id>:FY<year>`, which collided across
companies and made `GET /sources` ambiguous once comparison put two companies' metrics in one
evidence set. No schema change; the resolver matches whatever `source_id` is stored.

Valuation multiples (ADR-0036) are stored twice: per fiscal year (`period_type` FY, priced at the
year end) and at the latest close (`period_type` LATEST, `period` = the price date, source ID
`derived:<TICKER>:<metric>:<date>`); the latest-close rows are rebuilt with every price run.
Since Phase 7h (ADR-0046) the latest-close rows use TTM figures as filed by the price date (P/E =
market cap / TTM net income, `quality_flags.basis.figures` = "TTM to <quarter end> …"); fiscal-year
rows keep that year's figures. Composite tags (`composite:depreciation_amortization` for AMD and
Intel, from `concept_overrides/*.yaml` `composite_tags`) are summed per period and filing at load
time; `minority_interest` and `preferred_stock` are new stock metrics for enterprise value.

Base metrics and derived fourth quarters follow the same rule (ADR-0034): a base annual metric is
`xbrl:<TICKER>:<metric>:FY<year>` and a derived Q4 is `derived:<TICKER>:<metric>:Q4-FY<year>`. A base
metric used to carry its filing's accession number, which every metric from that filing shared;
the filing now lives only in `as_reported_accession`. Run `scripts/build_metrics.py` to apply the
scheme to an existing database.

### Quarterly and TTM rows (Phase 7b, ADR-0040)

`financial_metrics` holds the last 12 fiscal quarters (`period_type` Q, `period` `Q{n}-FY{yr}`)
and every complete trailing-twelve-month window (`period_type` TTM, `period` `TTM-Q{n}-FY{yr}`,
`period_end` = the last quarter's end). No new column. Source IDs:

| Row | `source_id` |
|-----|-------------|
| a quarter read directly from a filing (10-Q quarter or Q1 YTD, quarter-end balance) | `xbrl:<TICKER>:<metric>:Q<n>-FY<yr>` |
| a computed quarter (de-cumulated from YTD, Q4 = FY − ΣQ, quarterly total debt) | `derived:<TICKER>:<metric>:Q<n>-FY<yr>` |
| a TTM figure (four-quarter sum, TTM EPS, TTM margin/ROIC) | `derived:<TICKER>:<metric>:TTM-Q<n>-FY<yr>` |

Derived rows keep the existing record: `formula_id` (`ytd_decumulation`, `q4_derivation`,
`ttm_sum`, `ttm_eps_identity`, `gross_margin`, …), `quality_flags.derived` (the formula text,
e.g. `Q3 = 9M YTD - 6M YTD`) and `quality_flags.inputs` (each input's name and source ID). A
quarter's inputs cite the raw fact (`xbrl:<financial_facts.id>`) and the annual figure its own
row (`xbrl:<TICKER>:<metric>:FY<yr>`); a TTM's inputs cite the stored quarters. A TTM EPS summed
or computed across a >5% diluted-share change carries `quality_flags.quality_flag =
"share_count_changed"` (also in `warnings`). Rows are as-reported (earliest filing unless an
amendment, DR-QT-001), so Q1+Q2+Q3+Q4 equals the FY row exactly (DR-QT-004).

`metric_gaps (company_id, metric, period_label, reason, detected_at)`, unique
`(company_id, metric, period_label)`, `ON DELETE CASCADE` with the company: one row per quarter
or TTM window a metric is missing, with a reason code (`missing_ytd_1q|2q|3q`, `missing_fy`,
`missing_q1|2|3`, `missing_balance`, `not_additive`, `period_not_reported`,
`incomplete_window`). Rebuilt on every metrics run (delete-per-company + insert). A metric the
company never reports, or one not applicable to its sector (`sector_applicability.yaml`, DR-042),
has no gaps: N/A is not missing data.

### Earnings-quality rows (Phase 7d, ADR-0042)

`scripts/build_metrics.py` and the metrics task run `quality_builder` after valuation. It stores
one derived FY row per fiscal year for each of `accruals_ratio`, `accruals_ratio_bs`,
`fcf_conversion`, `dso`, `dio`, `dpo`, `cash_conversion_cycle`, `dilution`, `beneish_m`,
`altman_z` and `piotroski_f`, with source ID `derived:<TICKER>:<metric>:FY<yr>`. These are
ordinary derived rows: `formula_id` (`beneish_m_score`, `altman_z_score` or
`altman_z_double_prime`, `piotroski_f_score`, …) and `quality_flags.inputs`. Since Phase 7d every
derived row's `inputs` entry also carries the input's `value` as a string. Two-year inputs are
named `prior_<metric>`. Score details sit beside the lineage in `quality_flags`:

| Metric | Extra `quality_flags` keys |
|--------|---------------------------|
| `beneish_m` | `band` (`below_threshold`, `above_threshold`, `insufficient_data` or `not_applicable`), `threshold` (`-1.78`), `indices` (the eight, as strings), `missing` (absent inputs or `<index>: zero base`) |
| `altman_z` | `band` (`distress`, `grey` or `safe`), `model_variant` (`original` or `z_double_prime`), `variant_basis` (the SIC reason) |
| `piotroski_f` | `criteria` (nine booleans; null = not computable), `computable` (0–9) |
| `dilution` | `prior_shares_basis` (`restated in the FY<yr> 10-K`, citing the raw fact `xbrl:<financial_facts.id>`; or the split-adjusted fallback with `split_factor`) |

Uncomputable rows are NULL with `MISSING_INPUT` or `NEGATIVE_BASE` in `warnings`. Rows not
applicable to the sector (banks: Beneish, Altman, Piotroski, balance-sheet accruals, DSO, DIO, DPO,
CCC) are NULL with `NOT_APPLICABLE_SECTOR`. The TTM rows gain `fcf_conversion` and
`accruals_ratio` (`derived:<TICKER>:<metric>:TTM-Q<n>-FY<yr>`). The TTM accruals ratio averages
total assets at the end of the quarter before the window and at the window's end. There is no
schema change and no migration.

### Footnote facts (Phase 7f, ADR-0044)

`footnote_builder` runs after the ratio builder (metrics task, EDGAR poller,
`scripts/build_metrics.py`) and stores FY rows for the `footnote_metrics` of
`concept_map_v1.yaml`: debt maturities `debt_maturity_y1` … `y5` and `debt_maturity_after`,
`debt_weighted_avg_rate`, `revolver_capacity`, operating/finance lease liabilities, lease term and
discount rate, lease payments `lease_payments_y1` … `after` and `_total`, the reported effective
tax rate, unrecognized tax benefits, valuation allowance, purchase obligations, legal accrual and
unrecognized stock-compensation cost. Source ID `xbrl:<TICKER>:<metric>:FY<yr>`;
`quality_flags.note_category`, plus `source: "inline_xbrl"` when read from the 10-K's inline XBRL
(latest year only). A ladder bucketed from per-instrument inline facts (NVIDIA) is derived
(`derived:<TICKER>:debt_maturity_<w>:FY<yr>`, lineage = the notes). Derived
`debt_due_within_2y_share` and `lease_adjusted_debt` carry `quality_flags.derived` (the formula)
and `inputs`. The builder deletes and rewrites its own rows and its own `metric_gaps` (latest year:
`not_tagged`, `filing_unavailable`, `MISSING_INPUT`) on every run. No schema change, no migration.

### Documents: types, metadata and parsing (Phase 7c, ADR-0041)

`documents.document_type` values: `filing` (10-K, 10-Q, 8-K, 8-K/A, DEF 14A, DEFA14A),
`late_filing_notice` (NT 10-K, NT 10-Q: stored and given metadata, never chunked or embedded),
`earnings_release` (an 8-K's EX-99.x press release, `source_tier` 2, `published_at` = the 8-K's
EDGAR acceptance time), and `news`. A filing's `published_at` is also its acceptance time.

`documents.metadata` keys:

| Key | On | Meaning |
|-----|----|---------|
| `form`, `accession_number` | filings | as filed |
| `items_metadata` | 8-K, 8-K/A | the filing index's own item list (NULL when the index gave none) |
| `items_parsed` | 8-K, 8-K/A | the dotted item headings found in the text at parse time |
| `items_mismatch` | 8-K, 8-K/A | true when the two lists differ (both kept; the index list is what `filings.items` holds) |
| `exhibits_checked` | 8-K with Item 2.02 / 7.01 / 8.01 | its filing index was read once, so it is not re-read every run |
| `supplemental` | DEFA14A | additional proxy soliciting material |
| `notice_for`, `period_end` | late_filing_notice | the form it is late for (`10-K` / `10-Q`) and its period |
| `parent_accession`, `exhibit_type`, `filename`, `size_bytes` | earnings_release | the 8-K it came with, `EX-99.1` etc. |
| `notes` | 10-K, 10-Q (Phase 7f) | `{taxonomy: "note_taxonomy_v1", low_confidence, items: [{note_id, number, title, category, also_matches, paragraph_ids}]}`, written at parse time (ADR-0044) |
| `proxy_sections` | DEF 14A, DEFA14A (Phase 7g) | governance sections found at parse time (`board`, `summary_compensation_table`, …) |
| `proxy` | DEF 14A (Phase 7g) | `fiscal_year` and `fiscal_year_source` (from content), `peo_names`, `company_selected_measure`, `sct_rejected[]`, `sct_source_id`, `pay_ratio_status` |
| `governance_facts` | DEF 14A (Phase 7g) | cached `governance_v1` fields keyed `{prompt_version}|{model}`: `{value, quote, paragraph_id}` or null each, `rejected[]` |
| `note_summaries` | 10-K (Phase 7f) | cached `note_summary_v1` output keyed `{note_id}|{prompt_version}|{model}`; dropped on re-parse because it cites chunk IDs |

`documents.parser_version` is NULL until the document is parsed, then the parser version that
produced its chunks (`sec-ingest-v5` since 2026-10-31: v3 starts a chunk at every note heading in
the financial-statement sections, v4 splits proxy statements into keyword sections, v5 breaks lines
only at block elements and sections annual-report layouts by their own headings, ADR-0052); the parse step skips documents already at it. A re-parse
replaces every chunk of the document, whatever its version, keeping the embedding of any chunk
whose `content_hash` is unchanged. `document_chunks.paragraph_id` is `{document}_{item}_p{n}` for
text (`_p` since v5) and `{document}_{item}_{table_id}` for tables, where 8-K items are dotted
(`5.02`) and 10-Q items part-qualified (`P1-1`), so a text chunk ID from an older parse never
resolves to other text. `chunk_metadata.section_key` names the section independently of
its heading (`10q_part1_item1_notes`, `10k_item7`, `8k_item2.02`, `document`); exhibit chunks
carry `filing_type` = the exhibit type (`EX-99.1`) and `tier` 2. No schema change.

### Company profile enrichment (Phase 6a)

`companies.sic_code`, `sector`, `industry`, `exchange`, and `fiscal_year_end` were nullable and
left NULL by Phase 1a. SEC ingestion now populates them from the EDGAR `submissions` endpoint
(`sector` is derived from the SIC code via `app/finance/sic.py`); `scripts/enrich_companies.py`
backfills them without re-downloading filings. These feed peer-set grouping (§37.1) and portfolio
sector exposure (§37.2). No schema change — the columns already existed. `upsert_company` now
preserves a non-NULL profile value on a metadata-less re-ingest (`COALESCE(excluded, existing)`).

## Index rationale

| Table | Index | Why |
|-------|-------|-----|
| `document_chunks` | `embedding` **HNSW** (`vector_cosine_ops`, m=16, ef_construction=64) | Approximate nearest-neighbour vector search for RAG (RAG-010, NFR-002); cosine matches Voyage embeddings (ADR-0010) |
| `document_chunks` | `tsv` **GIN** | Keyword search over the generated `tsvector`; the other half of hybrid retrieval |
| `document_chunks` | btree `(company_id, filing_type, filing_date)` | Filter candidates by company + filing before/after vector search |
| `financial_facts` | btree `(company_id, concept_tag, period_end)` | Look up a concept's value for a period when computing metrics |
| `financial_metrics` | btree `(company_id, metric_name, is_latest)` | Fetch the current value of a metric for the dashboard fast (FR-002, NFR-001) |
| `price_history` | unique `(company_id, date)` | One bar per day; makes daily upserts idempotent (NFR-014) |
| `news` | btree `published_at` | Recent-news ordering on the request path (FR/NFR-006) |
| `refresh_tokens` | btree `family_id`, unique `token_hash` | Rotation/reuse detection and O(1) token lookup (ADR-0005) |
| `auth_tokens` | unique `token_hash`, btree `user_id` | O(1) single-use token lookup; per-user rate limit on issuance (AUTH-003) |
| FKs | btree on every foreign key column | Avoids sequential scans on joins and on cascade deletes |

### Uniqueness / idempotency constraints

- `companies.cik` unique; `filings.accession_number` unique; `users.email` unique.
- `documents (company_id, content_hash)` unique — re-ingesting the same document is a no-op.
- `document_chunks (document_id, chunk_index, parser_version)` unique — safe re-chunking.
- `financial_metrics (company_id, period, metric_name, basis, as_reported_accession)` unique —
  one canonical value per as-reported period.
- `metric_gaps (company_id, metric, period_label)` unique — one reason per missing quarter or
  TTM window (FR-QT-010); its leading `company_id` also serves the per-company delete.
- `notifications (alert_id, event_key)` unique (`uq_notifications_alert_id_event_key`) — the
  alert-dispatch idempotency guard (FR-AL-007, ADR-0039). Dispatch inserts with
  `ON CONFLICT DO NOTHING` and emails only rows it actually created, so a re-run, an overlapping
  run, or a run whose watermark update failed never delivers an event twice.

### Red flags (Phase 7e, ADR-0043)

`red_flags (id, company_id, rule_key, taxonomy_version, filing_id, document_id, paragraph_id NULL,
detected_at, event_date, severity, snippet, payload JSONB)`. Each row is one rule from
`red_flag_taxonomy_v1` (`docs/red-flag-taxonomy.md`) firing on one filing:
- The unique key is `uq_red_flags_event (company_id, rule_key, taxonomy_version, filing_id,
  paragraph_id)` with **NULLS NOT DISTINCT**, so a flag that quotes no paragraph (a late-filing
  notice) is still deduplicated. Detection inserts `ON CONFLICT DO NOTHING`, so re-runs are no-ops.
- A new taxonomy version adds rows and leaves the old ones for audit. Readers filter on the
  current version.
- `detected_at` is the first detection of the event under any version (copied forward), and the
  `red_flag` alert keys off it.
- `event_date` is the filing's date, never the detection time.
- `snippet` is the filing's own words.
- `payload` holds rule details: `item`, `roles`, `classification`, `restated[]`, or Beneish
  `score`/`fiscal_year`/`source_id`.
- `company_id`, `filing_id` and `document_id` are `ON DELETE CASCADE`. Index:
  `(company_id, event_date)`.
- Each detection pass records an `ingestion_runs` row (source `red_flags`), whose `ended_at` is
  the "scanned through" date. Source ID: `redflag:<TICKER>:<rule_key>:<accession>`.

### Executive compensation facts (Phase 7g, ADR-0045)

`exec_comp_facts (id, company_id, document_id, fiscal_year, tier, fact_key, person NULL, role NULL,
value NUMERIC(28,8), unit, source_id, created_at)`. Each row is one validated figure from one proxy
statement (`document_id` is the DEF 14A):
- `tier` `pvp`: a Pay Versus Performance fact (`fact_key` `peo_total_comp`,
  `peo_actually_paid_comp`, `non_peo_neo_avg_total_comp`, `non_peo_neo_avg_actually_paid_comp`,
  `tsr`, `peer_group_tsr`, `company_selected_measure`, `net_income`), five years per proxy.
  `source_id` is `xbrl:<financial_facts.id>`, or the PvP table chunk when only the proxy's
  Inline XBRL has it. TSR rows have unit `index` (the worth of $100 invested).
- `tier` `sct`: a Summary Compensation Table cell (`salary`, `bonus`, `stock_awards`,
  `option_awards`, `non_equity_incentive`, `pension_nqdc`, `all_other`, `total`) with `person`
  (name and position as printed) and `role` (`peo`, `pfo` or `neo`), cited to the table chunk.
  Only rows whose components sum to the total within $1 are stored.
- `tier` `pay_ratio`: `ratio` (unit `ratio_to_1`) and `median_pay`, stored only if median ×
  ratio is within 2% of the CEO's total, cited to the paragraph.
- `fiscal_year` is the year the figure covers, never the filing year. Missing figures are absent
  rows, never zeros.
- Unique `uq_exec_comp_facts_fact (document_id, fiscal_year, fact_key, person)` NULLS NOT
  DISTINCT. The builder deletes and reinserts a proxy's rows, so rebuilds are idempotent. FKs
  `ON DELETE CASCADE`; index `(company_id, fiscal_year)`.

### Peer sets and valuation history (Phase 7h, ADR-0046)

- `peer_sets (id, owner_user_id NULL, company_id, name, kind, created_at)`: `kind`
  `system_default` (one per company, `owner_user_id` NULL, rebuilt with prices and by
  `scripts/build_metrics.py`) or `user` (private to its owner; the API answers 404 to anyone else).
  Index `(company_id, owner_user_id)`.
- `peer_set_members (id, peer_set_id, peer_company_id, rationale, position)`: one row per peer,
  unique `(peer_set_id, peer_company_id)`. `rationale` is "same SIC 3674; market cap 1.8× target"
  for the default rule, "chosen by you" for a user's set; `position` keeps the rule's order. A set
  with fewer than three members is "insufficient peers".
- `valuation_history (id, company_id, as_of_date, kind, fiscal_year, multiple, value NULL, status,
  inputs JSONB, created_at)`: every multiple (`pe_ratio`, `ps_ratio`, `ev_revenue`, `ev_ebitda`,
  `ev_fcf`, `pb_ratio`, `fcf_yield`, `earnings_yield`) at each of the last five fiscal year ends
  (`kind` fiscal_year_end) and the latest close (`current`), computed only from facts filed by
  `as_of_date`. `status` is `ok`, `not_meaningful` (zero or negative denominator, or over 200x),
  `missing_input` or `not_applicable`; `value` is NULL unless ok. `inputs` holds `quarter_end`, the
  price (`price:<TICKER>:<date>`), the share count (`xbrl:<fact id>`) and each figure with its
  `source_ids`. Unique `(company_id, as_of_date, multiple)`; rebuilt per company on every price run
  and metrics build.
- All FKs `ON DELETE CASCADE`.

### Guidance and outcomes (Phase 7i, ADR-0047)

- `guidance_items (id, company_id, source_document_id, paragraph_id, issued_at, fiscal_year NULL,
  fiscal_period NULL, metric, low NULL, high NULL, point NULL, unit NULL, basis NULL,
  derived_range, quote, period_text NULL, confidence_state, rejection_reason NULL, superseded_by
  NULL, taxonomy_version, prompt_version, created_at)`: one figure a company guided to in an
  earnings release (`documents.document_type = 'earnings_release'`, EX-99.1/EX-99 only).
  `confidence_state` is `verified` (shown) or `rejected` (kept with its `rejection_reason` for
  audit, never shown; period, unit and basis may be NULL). `metric` is from `guidance_metric_v1`;
  `unit` `usd | ratio | percent_of_revenue`, money raw USD, ratios decimals; `basis`
  `gaap | non_gaap | unspecified`, set by keyword, not by the model. CHECK `one_value_form`: a
  verified row holds `point` or both `low` and `high`, never both forms. `derived_range` marks a
  range computed in code from "X, plus or minus Y". `superseded_by` (self FK, `ON DELETE SET NULL`)
  points at the next release's item for the same `(fiscal_year, fiscal_period, metric, basis)`;
  an item nothing points at is the initial guidance (the headline). `issued_at` is the release's
  `published_at`. Indexes `(company_id, fiscal_year)` and `(source_document_id)`. A document's rows
  are replaced together when it is re-extracted; the extraction cache sits in
  `documents.metadata.guidance_extraction["<prompt>|<model>"]`.
- `guidance_outcomes (guidance_id PK/FK, actual_value NULL, actual_source_ids JSONB, delta_vs_low,
  delta_vs_high, delta_vs_mid, delta_vs_mid_pct, result, reason_code NULL, computed_at)`: each
  verified item against the same fiscal period's GAAP `financial_metrics` rows. `result` (CHECK
  `result_known`) is `above_range | within_range | below_range | not_comparable |
  actual_unavailable`; the last two leave `actual_value` and the deltas NULL with a `reason_code`
  (`non_gaap_basis`, `basis_unspecified`, `actual_not_reported`), never zero. `actual_source_ids`
  are the metric rows' own source IDs (a ratio lists both inputs). Upserted on every metrics build;
  `computed_at` moves only when the result or actual changes, which is what the `guidance_miss`
  alert keys on.
- FKs to companies and documents `ON DELETE CASCADE`; an outcome goes with its item.

### Earnings calendar and briefs (Phase 7j, ADR-0048)

- `earnings_events (id, company_id, fiscal_year, fiscal_period, report_date NULL, report_time,
  source, status, source_id NULL, quote NULL, created_at)`: one row per fiscal quarter (unique
  `uq_earnings_events_period (company_id, fiscal_year, fiscal_period)`), upserted each time the
  calendar is built. `source` (CHECK `source_known`) is `sec_8k` (an Item 2.02 8-K was filed;
  `source_id` `filing:<id>`), `announcement` (the company's own sentence, kept in `quote`;
  `source_id` its paragraph ID) or `estimated` (last year's date for the quarter plus 52 weeks;
  shown with "~", never notifies). `status` (CHECK `status_known`) is `reported | scheduled |
  unknown`; `unknown` leaves `report_date` NULL. `report_time` (CHECK `report_time_known`) is
  `bmo | amc | unknown`. Fiscal period is stored apart from the calendar date (it is never
  inferred from it).
- `earnings_briefs (id, company_id, fiscal_year, fiscal_period, kind, version, inputs_hash,
  inputs_label, report_date_estimated, content JSONB, prompt_version, model NULL, created_at)`:
  one row per version of a preview or brief. `kind` (CHECK `kind_known`) is `preview | brief`.
  `inputs_hash` is the sha-256 of the assembled sections (every input's IDs and values), unique
  per `(company_id, fiscal_year, fiscal_period, kind, inputs_hash)` (`uq_earnings_briefs_inputs`):
  the same inputs return the stored row; new inputs add `version` n+1 (unique
  `uq_earnings_briefs_version`), and old versions are kept for audit. `content` is the
  `EarningsPreview`/`EarningsBrief` schema (money raw USD as strings, ratios decimals).
  `report_date_estimated` lets the `brief_ready` detector skip previews of estimated dates.
- FKs to companies `ON DELETE CASCADE`.

### Investment theses (Phase 7k, ADR-0049)

Private, user-owned rows. Every API read filters by the signed-in user (another user's row is a
404), and deleting a thesis, or the user (`hard_delete_user`), removes everything below by FK
cascade. Thesis text is never written to logs or metrics (SEC-TH-002).

- `theses (id, user_id, company_id, title, body_md, stance, status, created_at, updated_at)`:
  `stance` (CHECK `stance_known`) `long | short | watch | avoid` is the user's own tag; `status`
  (CHECK `status_known`) `active | closed`. Indexes `(user_id, status)` and `(company_id)`. At
  most 20 active per user, `body_md` at most 20,000 characters (enforced by the API).
- `thesis_kpis (id, thesis_id, metric, period_type, comparator, threshold, unit, label,
  rationale NULL, state, last_value NULL, last_source_ids JSONB, last_period NULL,
  last_period_end NULL, evaluated_at NULL, created_at)`: `metric` is a key of
  `thesis_metric_registry_v1`, validated at write, with `unit` its canonical unit (ratios as
  decimals). `comparator` (CHECK `comparator_known`) `gte | lte | gt | lt`; `threshold`
  NUMERIC(28,8). `state` (CHECK `state_known`) `met | broken | no_data`, with the value, source
  IDs and period it rests on. `evaluated_at` NULL means "no baseline yet": the next evaluation
  records a state without an event. At most 10 per thesis.
- `thesis_events (id, thesis_id, kpi_id NULL, kind, occurred_at, ref_type NULL, ref_id NULL,
  note NULL, payload JSONB)`: the append-only timeline.
  - `kind` (CHECK `kind_known`) is `kpi_met | kpi_broken | red_flag | brief_published |
    user_note | status_changed`.
  - KPI events are written only on a data-driven state change; their `payload` holds the value,
    period, `period_end`, source IDs, comparator and threshold.
  - `ref_id` is a backend source ID (`derived:…`, `redflag:…`, `earnings_brief:{id}`).
  - A partial unique index `uq_thesis_events_link (thesis_id, kind, ref_id) WHERE kind IN
    ('red_flag', 'brief_published')` links each flag or brief once.
  - `kpi_id` is `ON DELETE SET NULL`, so a removed KPI's history stays.
- `thesis_revisions (thesis_id, revision, title, body_md, created_at)`: PK `(thesis_id,
  revision)`, numbered from 1; every change of title or body adds one, nothing is overwritten.

### Alerts and notifications (Phase 7a, ADR-0039)

`alerts`: `event_type` (a key in `app/alerts/registry.py`), `params` JSONB (validated against that
type's params model; `{}` = defaults), `channels` `VARCHAR(16)[]` (subset of `in_app`, `email`),
`enabled`, `company_id` (nullable: NULL means every company on the owner's watchlist, read at
dispatch time), `created_at`, and `watermark_at TIMESTAMPTZ NOT NULL`. Dispatch only considers
events with `watermark_at < occurred_at <= run_started_at` and then sets `watermark_at` to the
run's start. A new alert's `watermark_at` equals its `created_at`, so it never replays history.

`notifications` adds `event_key` (deterministic, e.g. `new_filing:NVDA:<accession>`; a digest
uses `digest:<hash>`) and `payload` JSONB `{event_type, severity, source_ids, event_keys}` — a
digest's `event_keys` lists every event it covers.

## ON DELETE behaviour (DB-002)

Deleting a **company** cascades to all its data (documents, filings, chunks, facts, metrics,
prices, news, ownership, reports). Deleting a **user** cascades to that user's rows (refresh
tokens, watchlists, alerts, chat, feedback) and sets `research_reports.user_id` to NULL (the
report survives as system-owned). `news.document_id`, `filings.amends_filing_id`,
`research_reports.supersedes_report_id`, and `refresh_tokens.replaced_by_id` use `SET NULL`.
User-data hard-deletion for privacy requests (LGL-007) is a Phase 4b job.

## Working with the schema

```bash
# from repo root, with Docker running
make up                                   # Postgres + Redis
cd backend
uv run alembic upgrade head               # apply migrations
uv run python ../scripts/seed.py          # 3 companies + placeholder rows (idempotent)
uv run alembic downgrade base             # tear the schema back down
```

Migrations are tested up and down, on empty and populated databases, in
`backend/tests/integration/test_migrations.py`.

### Migration history

| Revision | Change |
|----------|--------|
| `fd52b5f9aeae` | Initial schema (all 33 tables, pgvector, indexes). |
| `d5095d7bd954` | Widen `financial_facts.concept_tag` to `VARCHAR(256)` — some real XBRL element names exceed 128 chars (Phase 1a ingestion). |
| `7ce7bee338f7` | Make `financial_metrics.metric_value` nullable — a sector-inapplicable or uncomputable metric stores NULL + a reason in `quality_flags`, never a misleading zero (Phase 1c, DR-041/042). |
| `8c58e66dbdbc` | Add `document_chunks.embedding_dim` (per-row dimension beside `embedding_model`, EMB-001) and a nullable `embedding_new vector(1024)` staging column for zero-downtime model migration (Phase 2b, EMB-003, ADR-0013). |
| `723724f0d34e` | Add the `auth_tokens` table (single-use, hashed email-verify / password-reset tokens, AUTH-003) and `users.deleted_at` (soft-delete marker set by `DELETE /auth/me`; hard-delete via a retention job, LGL-007). Phase 4b. |
| `331b479d2be6` | Add `llm_calls.user_id` (nullable FK, `ON DELETE SET NULL`) — attributes each LLM call to its requesting user so `app.repositories.llm_calls.monthly_spend` can enforce the per-user AI budget (NFR-008). Nullable because embedding-batch calls happen at ingestion time with no requesting user. Phase 5c, ADR-0022. |
| `8b0a53766648` | Add the `notifications` table (in-app records written by alert dispatch; `alert_id`/`company_id` `ON DELETE SET NULL`). Phase 5d, ADR-0025. |
| `e4a7c2d91b30` | Alert framework v2: `alerts.alert_type/config/is_active/channel` → `event_type/params/enabled/channels[]`, `company_id` nullable, drop `last_triggered_at`, add `created_at` + `watermark_at` (existing rows backfilled with `now()` at migration time so deploying replays nothing); `new_10k/new_10q/new_8k` become `new_filing` with a `form_types` param. `notifications` gains `event_key`, `payload` and unique `(alert_id, event_key)`. Downgrade drops watchlist-wide alerts and keeps only the first form/channel. Phase 7a, ADR-0039. |
| `b7d3e9f1a2c4` | Add the `metric_gaps` table (`company_id` FK `ON DELETE CASCADE`, `metric`, `period_label`, `reason`, `detected_at` default `now()`, unique `(company_id, metric, period_label)`): quarters and TTM windows a metric could not be computed for, and why. Phase 7b, FR-QT-010, ADR-0040. |
| `c3f8a1d2e9b7` | Add `red_flags` (FKs to companies, filings, documents `ON DELETE CASCADE`; unique `uq_red_flags_event (company_id, rule_key, taxonomy_version, filing_id, paragraph_id)` NULLS NOT DISTINCT, named explicitly because the generated name passes Postgres's 63 characters; index `(company_id, event_date)`). Phase 7e, FR-RF-006, ADR-0043. |
| — | Phase 7f adds no migration: notes and summaries live in `documents.metadata`, footnote facts in `financial_metrics`/`metric_gaps` (ADR-0044). |
| `f2a9c4e7b1d3` | Add `exec_comp_facts` (FKs to companies and documents `ON DELETE CASCADE`; unique `uq_exec_comp_facts_fact (document_id, fiscal_year, fact_key, person)` NULLS NOT DISTINCT; index `(company_id, fiscal_year)`). Phase 7g, FR-GV-004, ADR-0045. |
| `a6d2f8c3e5b1` | Add `peer_sets`, `peer_set_members` (unique `(peer_set_id, peer_company_id)`) and `valuation_history` (unique `(company_id, as_of_date, multiple)`); FKs `ON DELETE CASCADE`. Phase 7h, FR-PS-001/008, ADR-0046. |
| `b9e4d7a2c6f1` | Add `guidance_items` (FKs to companies and documents `ON DELETE CASCADE`, self FK `superseded_by` `ON DELETE SET NULL`, CHECK `one_value_form`, indexes `(company_id, fiscal_year)` and `(source_document_id)`) and `guidance_outcomes` (PK/FK `guidance_id` `ON DELETE CASCADE`, `actual_source_ids` JSONB default `[]`, CHECK `result_known`). Phase 7i, FR-GD-001/002, ADR-0047. |
| `f186174c8045` | Add `earnings_events` (unique `uq_earnings_events_period`, CHECKs `status_known`, `source_known`, `report_time_known`) and `earnings_briefs` (unique `uq_earnings_briefs_inputs` and `uq_earnings_briefs_version`, CHECK `kind_known`, `content` JSONB); FKs to companies `ON DELETE CASCADE`. Phase 7j, FR-EB-001/007, ADR-0048. |
| `69ec64a10d9e` | Add `theses` (FKs to users and companies `ON DELETE CASCADE`, CHECKs `stance_known`/`status_known`), `thesis_kpis` (CHECKs `comparator_known`/`state_known`), `thesis_events` (CHECK `kind_known`, `kpi_id` `ON DELETE SET NULL`, partial unique `uq_thesis_events_link`) and `thesis_revisions` (PK `(thesis_id, revision)`); child FKs `ON DELETE CASCADE`. Phase 7k, FR-TH-001, ADR-0049. |
