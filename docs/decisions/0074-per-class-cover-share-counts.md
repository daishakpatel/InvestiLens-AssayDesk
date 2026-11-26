# ADR-0074: Per-class cover-page share counts from inline XBRL, and stale counts are not used

- **Status:** Accepted
- **Date:** 2026-11-25
- **Spec refs:** §4.1, §11.6, DR-023, DR-025, FR-024, ADR-0046

## Problem

Adding the rest of the seed set (§4.1) showed that SEC companyfacts leaves out dimensioned facts,
and a multi-class filer reports its cover-page `dei:EntityCommonStockSharesOutstanding` only per
class. Alphabet and Meta had no share count at all, so no market cap or multiple. Visa's and
Mastercard's only un-dimensioned counts dated from 2010, and `count_as_of` took the latest count
with no age limit, so their 2026 market caps came out at $199B and $81B, about a fifth of the
real figure. Visa adds a second problem: its Class B-1/B-2/B-3 and C shares convert into the
traded Class A at their own, changing rates, so summing the classes misstates the count.

## Decision

SEC ingestion reads the inline XBRL of every stored 10-K and 10-Q that companyfacts has no cover
count for (`_cover_share_rows`, using `ixbrl.tagged_values`) and stores its counts in
`financial_facts`: the company total when the cover gives one, else one row per class with the
class in `dimensions` and the filing's own date as `filed_date`. `share_counts` already sums the
counts of one filing and date (DR-025). A company override can set `sum_share_classes: false`
(Visa, `concept_overrides/V.yaml`); its per-class rows are then ignored and its market cap stays
missing. `facts_for_company` leaves out dimensioned rows, so metric selection only sees company
totals. A share count more than 366 days from the date being valued is stale and not used, both
for the latest close (`count_as_of`) and for fiscal-year ends (`share_count_for`).

## Consequences

Alphabet, Meta and Mastercard get market caps and multiples from their cover pages; Alphabet's
summed count (12.294B on 2026-10-21) matches its balance-sheet total. Visa shows no market cap or
multiples until as-converted counts are modelled, which needs each class's conversion rate from
its filings. Re-ingesting a multi-class company reads about 15 stored filings (under 0.1 s each).
Fiscal years whose nearest cover count is over a year away now show multiples as missing instead
of using an old count.
