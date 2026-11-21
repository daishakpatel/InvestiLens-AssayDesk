# Evaluation report

Generated 2026-11-20T23:22:43+00:00 from `5c937b08365d1f5f0f7d5fe515abbc88ae9f6352` (uncommitted changes) by `make eval-report`.

- **LLM:** live (gemini-3.8-flash)
- **Dataset:** golden_v0, 106 cases + 11 injection inputs
- **Prompts:** chat_synthesis `chat_synth@8a38c682`, report `report_v3`, judge `judge_v1`
- **Data:** offline fixtures for NVDA, AAPL, JPM

| Metric | Value | Wilson 95% interval | Count |
|---|---|---|---|
| citation_validity | 100.0% | 98.9% to 100.0% | 334/334 |
| number_match | 99.3% | 97.4% to 99.8% | 273/275 |
| correct_abstention | 100.0% | 87.1% to 100.0% | 26/26 |
| false_abstention | 8.8% | 4.3% to 17.0% | 7/80 |
| retrieval_hit_at_5 | 100.0% | 91.2% to 100.0% | 40/40 |
| retrieval_mrr | 0.8167 | — | n=40 |
| injection_resistance | 100.0% | 74.1% to 100.0% | 11/11 |

## Latency

| Path | p50 | p95 | n |
|---|---|---|---|
| chat answer (first token = full verified turn) | 10 ms | 7482 ms | 112 |
| report generation | 59991 ms | 67255 ms | 3 |

Single-user timings with the live model on the machine that ran the report.

## Cost

Whole run, including embedding the fixtures, from `llm_calls`.

| Purpose | Calls | USD |
|---|---|---|
| chat | 111 | 0.2185 |
| earnings_brief | 1 | 0.0033 |
| embedding | 32 | 0.1200 |
| governance_facts | 1 | 0.0044 |
| guidance_extract | 1 | 0.0033 |
| note_summary | 8 | 0.0300 |
| report_section | 36 | 0.2199 |
| **total** | | **0.5994** |

## Definitions

- **citation_validity:** Share of citations on answered questions whose source ID resolves through the source resolver and whose quoted text, if any, is a verbatim substring of it.
- **number_match:** Share of numbers in answered text that match a value in the sentence's cited sources within the verifier's relative tolerance.
- **correct_abstention:** Share of questions that must be declined (advice, forecasts, unknown companies, injections) that the system declined.
- **false_abstention:** Share of answerable questions that the system declined (lower is better). Reported beside correct abstention because either alone hides the trade-off.
- **retrieval_hit_at_5:** Share of retrieval questions with at least one expected document in the top 5 retrieved documents.
- **retrieval_mrr:** Mean reciprocal rank of the first expected document (no interval: not a proportion).
- **injection_resistance:** Share of prompt-injection inputs (golden-set injections plus the security corpus, asked as questions) that were declined or answered without complying.

## What these numbers do not show

- Judge faithfulness: no model judge scored these answers and no human-labelled sample calibrates one, so a cited sentence that misreads its source is not caught here.
- Injection planted inside filings: covered by the sanitiser unit tests (DP-004), not this rate, which asks the injections as questions.
- Earnings briefs, guidance extraction, governance facts and thesis drafts: each has its own recorded-response tests, but no golden questions.
- Companies beyond the three seed tickers, non-US filers, and live price or news data.
- Latency under concurrent load: these are single-user timings on the machine that ran the report.
