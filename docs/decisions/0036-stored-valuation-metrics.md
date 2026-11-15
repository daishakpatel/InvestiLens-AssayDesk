# ADR-0036: Store valuation multiples per fiscal year and at the latest close

- **Status:** Accepted; the latest-close row's fiscal-year figures were replaced by TTM as filed
  by the price date in ADR-0046 (Phase 7h)
- **Date:** 2026-10-14
- **Spec refs:** §10.7, §11.6, §11.7, §12, FR-024, DR-025, DR-026, DR-042, CIT-003

## Problem

Market cap, enterprise value and the price multiples (P/E, P/S, P/FCF, EV/revenue, EV/EBITDA, FCF
yield) had tested formulas in `app/finance/metrics.py` but were never stored, because no real
prices existed. The company header, the comparison table, peer suggestions and the report's
valuation section were therefore empty. Two different "when" questions need answering: what a
company was worth at each fiscal year end (history, comparisons, the report), and what it is
worth now (the header).

## Decision

`app/finance/valuation_builder.py` writes both into `financial_metrics` as derived rows with
lineage. A **fiscal-year row** (`FY<year>`, `period_type` FY) uses the raw close on the last
trading day on or before the year end, the cover-page share count
(`dei:EntityCommonStockSharesOutstanding`, classes summed) reported nearest after it, and that
year's figures. A **latest-close row** (`period_type` LATEST, period = the price date, source ID
`derived:<TICKER>:<metric>:<date>`) uses the latest close, the latest share count and the latest
fiscal year's figures. Share counts and EPS are moved to the price date across any stock split
in between, using stored corporate actions. EV multiples are not applicable for banks. Each row
records its price date, close, share count, record date and fiscal year (FR-024). The rows are
rebuilt by `scripts/build_metrics.py`, the metrics jobs, and every successful price ingestion, so
the latest-close values move with prices. `get_latest_metrics` prefers a latest-close row.

## Consequences

The header shows today's market cap and P/E; comparisons, peer bands and the report get a five-year
history. The "TTM" inputs are fiscal-year figures until quarterly and TTM metrics are stored, so a
P/E late in a fiscal year uses earnings up to ten months old; each row's `basis` says so. A
latest-close row is replaced on every price run, so a report that cited yesterday's dated ID no
longer resolves it. Fiscal years that end before the stored price history are not valued.
EV/EBITDA is N/A wherever depreciation and amortization is not mapped (AMD and Intel today).
