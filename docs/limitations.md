# Limitations

What Assay Desk does not do, does badly, or has not proven. Each line is a specific statement
that can be checked against the code or the eval report, not a disclaimer. Updated 2026-10-25
(Phase 7l). The open-work list behind it is [`Instructions/TODO.md`](../Instructions/TODO.md).

## Data

- Financial statements come from SEC EDGAR XBRL company facts. A company that tags a line item
  with a custom or missing concept gets no value for it, shown as "not available" with a reason
  code, never 0. AMD's pretax income and AMD's and Intel's quarterly D&A are examples.
- Filings arrive when ingestion runs. Locally that means by hand. The scheduled poller (every 10
  minutes) and the 02:00 ET daily pipeline exist but have never run on a deployed stack.
- Prices are end-of-day only (Tiingo), refreshed after the US close and when the API starts.
  There are no intraday prices. The public demo shows fixture prices, because Tiingo's free
  licence forbids public display (ADR-0007).
- News is sample data: placeholder headlines from fixtures. Live news needs a Finnhub key that
  this project has not used, so every report's news summary is empty.
- Fiscal periods are taken from each filing's own dates, never inferred from calendar dates. A
  company with an unusual fiscal year (NVIDIA ends in late January) is labelled by its fiscal
  year, which can read as "a year ahead" of the calendar.

## Coverage

- The fixtures, the eval report and the public demo cover three companies: NVIDIA, Apple and
  JPMorgan. The development database adds AMD and Intel. Any other ticker must be ingested first.
- US SEC filers only. There are no IFRS filers, no foreign private issuers (20-F/6-K) and no
  non-US exchanges.
- Banks get no gross margin, inventory turnover or similar industrial ratios. These are marked
  "not applicable" by sector rules (DR-042). Bank-specific measures (net interest margin, CET1)
  are not computed.
- Some filings parse imperfectly. Intel's filings have no "Item N" headings and are sectioned by
  their own titles; its FY2021–2023 10-Ks still open with an unlabelled "Filing text" section.
  JPMorgan's 10-K and 10-Q name their items only in a cross-reference table and are sectioned by
  the report's own headings, a rule written for its current layout.
- Guidance is read only from ingested earnings releases (EX-99.1). An outlook laid out as a
  table (Intel's) is not extracted.

## Models and heuristics

- Beneish M-score, Altman Z and Piotroski F are textbook formulas with textbook cut-offs. They
  are not recalibrated for today's sectors or for companies like NVIDIA whose growth looks like
  manipulation to an accrual model. The app labels them screening heuristics, not findings
  (ADR-0042).
- Management-topic shifts use a fixed lexicon (`topic_taxonomy_v1`). They count words; they do
  not understand them. A topic described with words outside the lexicon is missed.
- Red flags are reported events (auditor changes, late filings, restatements, executive
  departures) matched by rules over filing text. The rules for going concern and material
  weakness have no real positive examples in the corpus, so their recall is unmeasured.
- Peer percentiles compare each company's latest fiscal year, not aligned calendar periods, and
  rest on small peer sets.
- Valuation multiples use the latest stored figures. Five-year average multiples are not built.

## AI

- **What is verified.** Every AI sentence must cite a backend-issued source ID, and every
  number in it must match a cited value within 1%. Unverifiable sentences are removed, not
  rewritten (CIT-005). The eval report measures this on what the user sees.
- **What is not verified.** Entailment is lexical (ADR-0015): a sentence that quotes its source
  but draws the wrong conclusion from it can pass. No model judge has been calibrated against
  human labels.
- **Abstention is uneven.** In the mock-LLM eval run (`docs/eval/latest.md`), off-topic prompts
  ("write me a poem about NVIDIA") were answered from loosely related filing passages instead of
  declined. A bank's "gross profit" question was answered from document text instead of
  "not applicable". Two of five injection questions got a cited, non-complying answer rather
  than a refusal.
- **Known routing gap.** Every "diluted EPS" question in the golden set was declined: the chat's
  metric lookup does not map that phrase to the stored `eps_diluted` values.
- **Mock-run limits.** The published numbers come from a deterministic fake model and hash
  embeddings, so they measure retrieval plumbing, verification and abstention, not writing
  quality or semantic search. No live-model eval has been published.
- **Narrow demo answers.** The public demo replays recorded answers only. A question nobody
  recorded gets the honest "not enough evidence" reply.

## Security and privacy

- **Stored:** account email, an Argon2id password hash, hashed refresh tokens, chat history
  (purged after the retention window, SEC-014), watchlists, alerts and theses. Theses are
  private to their owner; other users get 404 (SEC-TH-001).
- **Logged:** request IDs, routes, latencies, error classes and token counts. Thesis text, chat
  questions and draft notes are kept out of logs and metric labels (SEC-TH-002). Model prompts
  are not logged.
- **Not built:** no email delivery, so verification and password-reset tokens are created but
  never sent. No SSO, no 2FA, no audit log of who viewed what.
- **Rate limits** are in-memory per process unless `RATE_LIMIT_BACKEND=redis`. The brief
  regeneration and thesis-draft limits are always in-memory.

## Not built

- Analyst consensus estimates and "beat/miss vs the Street". There is no licensed source
  (ADR-0047), so guidance is compared only with the company's own outlook.
- Earnings-call transcripts and Q&A analysis (ADR-0011).
- Insider trades (Form 4) and institutional ownership (13F). Those endpoints return empty lists.
- Options data, short interest and intraday market data.
- DCF and scenario tools, a screener, exports beyond browser print and CSV.

## Known issues

Pulled from `Instructions/TODO.md`. Each links the task file whose area it belongs to; remove a
line when it is fixed.

| Issue | Status | Area |
|---|---|---|
| Live-model eval (Recall@5, judge faithfulness, cost) not published | Open: needs a paid run | [21 — evaluation](../Instructions/21_phase5b_evaluation_framework.md) |
| Load test never run against a deployed stack | Open | [22 — observability](../Instructions/22_phase5c_observability_and_security.md) |
| Public demo not yet deployed | Open: ready, needs the owner's Render account | [37 — this phase](../Instructions/37_phase7l_measured_results_and_polish.md) |
| News is sample data | Open: needs a Finnhub key | [09 — news](../Instructions/09_phase1e_news_ingestion.md) |
| No email delivery for verification and password reset | Open | [17 — auth](../Instructions/17_phase4b_auth_and_users.md) |

## How I'd validate this with a real analyst

- Pick 10 recent earnings releases across the seed companies. Have the analyst copy each
  outlook by hand, then compare item by item with what `guidance_extract_v1` stored: metric,
  range, basis (GAAP or non-GAAP) and fiscal period. Count misses and wrong periods separately.
- Take 30 AI sentences from three reports and have the analyst open each citation and mark it
  supports, partly supports or does not support. That gives an entailment error rate the
  lexical verifier cannot measure, and the 50-label set the judge needs for calibration.
- Recompute 20 headline figures (revenue, margins, EPS, free cash flow, P/E) in a spreadsheet
  from the same 10-K and 10-Q, and record every difference with its cause: tag choice, period
  or restatement.
- Run the red-flag rules over two years of 8-Ks for the five companies, have the analyst list
  the events they would flag, and measure what the rules missed and what they flagged that the
  analyst would not.
- Sit with the analyst for one earnings season using only the brief and the thesis tracker.
  Note every time they leave the app to check something elsewhere: each one is a missing
  feature or an untrusted number.
