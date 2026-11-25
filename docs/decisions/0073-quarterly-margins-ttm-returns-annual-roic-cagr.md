# ADR-0073: Quarterly margins, TTM returns, annual ROIC and 3-year revenue CAGR are stored

- **Status:** Accepted
- **Date:** 2026-11-23
- **Spec refs:** §10.2 ("3-Year CAGR"), §10.3, §37.7, DR-001, DR-042, DR-QT-003, ADR-0040

## Problem

Quarters stored only TTM gross, operating and FCF margins, ROIC and a few others, so the
Financials tab's Quarterly view hid its Margins charts and its TTM view showed no net margin, ROE
or ROA. No annual ROIC or revenue CAGR row existed (§37.7 leftovers).

## Decision

`quarterly_builder` stores each kept quarter's gross, operating and net margin from that
quarter's own figures, and adds TTM net margin, ROE and ROA (TTM net income over quarter-end
equity or total assets) to every TTM window; all cite their inputs and follow the existing rule
(stored when computable or N/A for the sector). A single quarter's ROE is not stored: one
quarter's profit over equity reads as a quarter of the annual figure, and TTM ROE covers the
trend. `ratio_builder` adds annual `effective_tax_rate` and `roic` (the year's tax rate and
balances) and `revenue_cagr_3y`, stored only once the year three back exists.

## Consequences

The Quarterly view now draws gross, operating and net margin, and the TTM view adds net margin,
ROE and ROA. A TTM window ending in Q4 equals its fiscal year to the digit (checked for NVIDIA and
Apple, and in a test). Banks get N/A, never a number, for gross margin and ROIC (DR-042).
Quarterly FCF margin is still not stored: free cash flow is only computed on a TTM basis.
