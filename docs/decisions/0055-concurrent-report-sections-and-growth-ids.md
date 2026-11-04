# ADR-0055: Concurrent report sections and resolvable growth citations

- **Status:** Accepted
- **Date:** 2026-11-03
- **Spec refs:** §10, §13.3, NFR-003, NFR-010, CIT-003, CIT-006

## Problem

Reports took 85–100 s against a 90 s target. Raising `LLM_RATE_LIMIT_PER_SEC` from 0.25 to 5 made
no real difference (mean 95 s against 98 s): the 12 section calls ran one at a time and took about
85% of each report, so the limiter rarely had to wait. Separately, reports cited
`derived:revenue_yoy_FY<year>` and `derived:revenue_cagr_FY<year>`. Those IDs were made up by the
report payload, named no company and matched no stored row, so `GET /sources/{id}` found nothing.

## Decision

Evidence is gathered in the calling thread as before. The section model calls then run in a thread
pool of six (`_SECTION_WORKERS`), and their results are read back in section order, so citation
numbering and `data_version` stay reproducible. A section's only session use is logging to
`llm_calls`, which takes a lock while the calling thread waits and leaves the session alone.
`RateLimiter` now reserves a slot under a lock, because parallel callers would otherwise all see
the same last-call time and ignore the limit.

Growth IDs become `derived:<TICKER>:revenue_<yoy|cagr>:FY<start>-FY<end>`. The resolver recomputes
the figure from those two years' stored revenue with the same `yoy_growth` and `cagr` functions, and
returns its inputs as lineage. Nothing is stored, so no rebuild is needed. IDs in reports generated
before this change still do not resolve.

## Alternatives considered

- **Raise the rate limit only:** measured above; no real gain.
- **Parallel retrieval too:** it shares the session and is a small part of the time.
- **One session per section:** each would commit its own log rows outside the report's transaction.
- **Persist CAGR in `financial_metrics`:** needs a metrics rebuild and a stored window choice for a
  figure only reports use, while a stored `yoy_growth` row cannot cover a gap between fiscal years.

## Consequences

With parallel calls, `LLM_RATE_LIMIT_PER_SEC` now sets report speed: at the default 0.25, twelve
calls take at least 44 s just to start. Measured at that default on `gemini-3.8-flash`: NVIDIA 75 s
and Apple 58 s. A faster limit was not measured, because the prepaid credit ran out during the run.
