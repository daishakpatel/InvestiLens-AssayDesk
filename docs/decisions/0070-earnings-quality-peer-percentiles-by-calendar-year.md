# ADR-0070: Earnings-quality peer percentiles compare one calendar year

- **Status:** Accepted
- **Date:** 2026-11-22
- **Spec refs:** §37.1, FR-EQ-010, DR-021, ADR-0042

## Problem

`/companies/{ticker}/earnings-quality` ranked the target's latest fiscal year against each peer's
latest year with a value, whatever year that was. NVIDIA's FY2026 (February 2025 to January 2026)
sat beside Apple's FY2026 (to September 2026), and a loss year with no FCF conversion was quietly
replaced by an older year (Intel FY2023 beside NVIDIA FY2026). The comparison engine already maps
fiscal years to calendar years (`app/comparison/calendar.py`); this endpoint did not use it.

## Decision

Each measure is compared in one calendar year: the target's latest calendar year (its N/A years
count), or the one before when the target has no value there or fewer than three peers have filed
it yet. Every company's rows are mapped with `calendarized` and its fiscal-year-end month; a peer
without a value in that year is left out, never replaced by another year. The response adds
`peer_calendar_year` (additive, nullable), and the card reads "75th percentile vs 3 peers
(calendar 2025)".

## Consequences

Percentiles compare like periods. Apple, whose September year runs ahead of December peers, is
compared on calendar 2025 until they file 2026, and says so. A measure can now be hidden where it
used to show a mismatched figure (FCF conversion for NVIDIA and AMD while Intel's 2025 is N/A).
Looking back more than one year is not attempted: an older comparison would read as current.
