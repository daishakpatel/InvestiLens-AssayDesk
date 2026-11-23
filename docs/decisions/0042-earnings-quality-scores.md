# ADR-0042: Earnings-quality scores: Beneish, Altman variants, Piotroski, and their inputs

- **Status:** Accepted
- **Date:** 2026-10-22
- **Spec refs:** §12, §37.7, Appendix C, Appendix H (FR-EQ-001 … FR-EQ-015, DR-EQ-001 … DR-EQ-004)

## Problem

Phase 7d needs three published screening models and several earnings-quality ratios, persisted
with their inputs and shown together. Each model comes in variants with different coefficients and
cut-offs, and a typo in either is the commonest bug. The models were also estimated on specific
populations, so we have to decide where they apply. We also need new XBRL inputs, and the existing
`metrics.py` versions were unguarded (Piotroski divided without checks and returned only a total;
Altman always used the manufacturer model).

## Decision

**Beneish M-score (8 variables):** `M = −4.84 + 0.920·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI +
0.115·DEPI − 0.172·SGAI + 4.679·TATA − 0.327·LVGI`, from Beneish, M. D. (1999), "The Detection of
Earnings Manipulation", *Financial Analysts Journal* 55(5): 24–36. Cross-checked on 2026-10-22
against the Wikipedia restatement (rev. 1369805175), which quotes the same coefficients and says:
"The threshold value is -1.78 for the model whose coefficients are reported above (see Beneish 1999,
Beneish, Lee, and Nichols 2013, and Beneish and Vorst 2020)." We use **−1.78**. A score above it is
`above_threshold`, a signal to investigate. The −2.22 cut-off some sources use comes from a
different cost ratio, so we don't use it.

We keep the 1999 index definitions with three choices:
- AQI = 1 − (current assets + net PP&E) / total assets. Optional securities are left out:
  NVIDIA's `AvailableForSaleSecuritiesDebtSecurities` includes current securities already in
  current assets, so adding it would double-count.
- TATA uses the cash-flow form, (net income − operating cash flow) / total assets, as Beneish,
  Lee & Nichols (2013) do.
- LVGI uses (current liabilities + long-term debt) / total assets.

**Altman Z:** the variant comes from the company's SIC code and is always stored as
`model_variant`:
- SIC 20–39 (manufacturers): the original Z, `1.2·WC/TA + 1.4·RE/TA + 3.3·EBIT/TA + 0.6·MVE/TL +
  1.0·Sales/TA`, with zones distress < 1.81, grey 1.81–2.99, safe > 2.99. Source: Altman (1968),
  *Journal of Finance* 23(4): 189–209.
- Other non-financials, or a company with no SIC code: Z″, `6.56·WC/TA + 3.26·RE/TA + 6.72·EBIT/TA +
  1.05·BookEquity/TL`, with no constant (the +3.25 belongs to the emerging-market variant) and zones
  distress < 1.10, grey 1.10–2.60, safe > 2.60. Source: Altman et al. (2017), *JIFMA* 28(2):
  131–171.

The market value of equity is the fiscal-year-end market cap that valuation already stores (ADR-0036).

**Piotroski:** all nine tests from Piotroski (2000) are exposed one by one (`None` means it could
not be computed). The 0–9 total is stored only when all nine are computable; otherwise the dashboard
reports "n of 9 computable".

**Applicability:** `sector_applicability.yaml` marks Beneish, Altman, Piotroski, balance-sheet
accruals and the working-capital days (DSO, DIO, DPO, CCC) not applicable for banks (DR-EQ-001).
Missing inputs or zero denominators give `insufficient_data` with the inputs named, never a zero
(DR-EQ-002).

**Other measures:**
- Balance-sheet accruals = ΔNOA / average NOA, where NOA = (TA − cash − short-term investments) −
  (TL − total debt). This is the CFA Institute aggregate-accruals form. Short-term investments
  reported in only one of the two years count as insufficient data, not as zero; that case is how
  the bug below showed up.
- Dilution compares diluted weighted shares against the prior year's count as **restated in the
  current 10-K**, which the filer has already moved across any split, and cites that exact raw fact.
  If that comparative is missing, it falls back to the as-reported count times the split factor
  from `corporate_actions`.

**Concept map:** the new tags are appended to `concept_map_v1.yaml`. The file's versioning rule
bumps on *changed* selections, and these are additions:
- `sga_expense`
- `depreciation` (marked `annual_only`: it is a 10-K note figure, so it has no quarters or gaps)
- `retained_earnings`
- `ppe_net` (falling back to the PP&E-plus-finance-lease tag Intel uses)

`short_term_investments` gains `us-gaap:DebtSecuritiesCurrent`, NVIDIA's tag from FY2026. It
fills a gap and changes no existing value. `receivables`, `current_assets`, `current_liabilities`,
`total_liabilities` and `income_tax_expense` already existed and are reused.

**Storage:** everything goes in `financial_metrics` as derived FY rows
(`derived:<TICKER>:<metric>:FY<yr>`). The lineage now also stores each input's value, so the
inputs disclosure can show the number it cites. Bands, indices, criteria and the model variant sit
in `quality_flags`. The TTM `fcf_conversion` and `accruals_ratio` are added to the 7b TTM builder.
There is no new table.

## Consequences

- The dashboard can show every number behind a score, and each one opens its source.
- A sentence citing a screening score with fraud or manipulation wording is rejected (`OVERREACH`)
  by the claim verifier. The report prompt says the same (`report_v3`).
- Costs:
  - Altman's original Z needs a price at the fiscal year end, so it is insufficient for years
    before the stored price history.
  - AMD and Intel do not tag total liabilities, so their Altman Z is insufficient (the missing
    input is named). Deriving it as assets − equity was rejected because temporary equity makes
    it inexact.
  - Peer percentiles compare each company's latest fiscal year, so a January year-end and a
    December year-end sit side by side, and they are withheld below three peers. (Superseded by
    ADR-0070: one calendar year, which the response names.)
- Revisit if:
  - we add banks-specific quality measures;
  - a licensed GICS mapping replaces SIC (the Altman variant would follow it);
  - Beneish is re-estimated (Beneish & Vorst 2020) with different coefficients.
