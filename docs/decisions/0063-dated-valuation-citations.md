# ADR-0063: A replaced latest-close valuation citation is rebuilt for its date

- **Status:** Accepted
- **Date:** 2026-11-15
- **Spec refs:** §13.3, CIT-002, CIT-006, FR-024; ADR-0036, ADR-0046

## Problem

A report cites the multiple at the latest close by a dated ID (`derived:NVDA:pe_ratio:2026-11-03`).
Every price run deletes and rewrites the latest-close rows under the new price date, so the next
day that ID no longer matched any row and `GET /sources/{id}` answered 404. On 2026-11-15 all 49
dated valuation IDs cited by stored reports were dead links.

## Decision

`GET /sources/{id}` rebuilds such an ID on request, like a revenue-growth ID (ADR-0055). The
latest-close calculation is now one function, `valuation_builder.valuation_at_close(company,
price)`, used both by the builder and by the resolver. For `derived:<TICKER>:<multiple>:<date>`
with a stored close on that date, it recomputes from what was known then: that close, the share
count filed by then, and TTM figures as filed by then. A dated ID that still has its stored row
resolves from the row, as before. IDs dated before `LATEST_TTM_SINCE` (2026-10-24, when ADR-0046
switched the latest-close row from fiscal-year figures to TTM) still answer 404: rebuilding them
under today's method would show a number the report never quoted (NVDA P/E 28.55 against the
46.64x a 2026-10-15 report cited). Not storing every day's rows keeps `financial_metrics` free of
a daily copy of eight rows per company and keeps `is_latest` meaning one thing.

## Consequences

Every post-switch citation in the dev reports now opens with the value its report quoted
(checked against the claim text for each report). Older ones stay unresolvable, as they were.
The rebuild is only as stable as its inputs: a later amendment filed after the date is ignored
(as-of filtering), but a change to the method itself would change old values again, so any
change to `valuation_at_close` must move `LATEST_TTM_SINCE` forward. Revisit by storing each
cited value with the report if that rule proves easy to miss.
