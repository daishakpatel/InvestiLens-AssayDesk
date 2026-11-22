# ADR-0069: The research report's 5-year average multiples

- **Status:** Accepted
- **Date:** 2026-11-22
- **Spec refs:** §10.7, FR-010, FR-024, CIT-003, CIT-006, ADR-0046, ADR-0055

## Problem

Spec §10.7 asks the valuation section for "Current P/E / 5Y Average P/E" (and P/S). The payload
gave the model each multiple's last five fiscal-year values and the latest close but no average,
so the model either left it out or would have had to compute it, which no financial number may be.
A loss year has no P/E (N/A, not zero), so a plain mean over five slots is undefined for Intel.

## Decision

`fin.average` (finance layer) is the mean of the years with a value and fails as `MISSING_INPUT`
below three. Each multiple in the valuation payload (unit `x`: P/E, P/S) gains `average`:
`{value, years, span, source_id}` over the five fiscal years the payload already shows, cited
`derived:<T>:<metric>_avg:FY<first>-FY<last>`, which the resolver recomputes from the stored FY
rows with the same function ("Price-to-earnings ratio average for FY2021 to FY2025 was 12.02x").
The span is the first to the last valued year, so Intel's P/E average reads FY2021 to FY2023
(three years) and says so.

## Consequences

Reports can state current-versus-average from backend figures with a citation that opens to its
inputs. It is a mean of fiscal-year-end multiples (point in time, ADR-0046), so one high year
(NVIDIA FY2023, 117x) pulls it up; `/valuation-bands` keeps the median and quartiles for that.
P/FCF, EV/Revenue and EV/EBITDA are not in the report's valuation section yet.
