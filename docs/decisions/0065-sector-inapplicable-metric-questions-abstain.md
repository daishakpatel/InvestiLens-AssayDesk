# ADR-0065: Questions about a sector-inapplicable metric abstain

- **Status:** Accepted
- **Date:** 2026-11-20
- **Spec refs:** §16.1, §18.1, DR-001, DR-042, RAG-030, NFR-ME-004

## Problem

The golden set labelled "What is JPMorgan's gross margin?" must-abstain (a bank has none, DR-042)
but "What drove the change in JPMorgan's gross margin?" (q_0044) and "Compare JPMorgan's gross
margin to its operating margin." (q_0082) answerable, because they came from templates applied to
every seed company. The pipeline only declined the numeric form (its stored metric is NULL); the
other two went to document search, where the mock model wrote an answer about a metric a bank
does not have and the live model declined, which the eval counted as false abstentions.

## Decision

`run_retrieval` declines, before any retrieval, a question whose named metrics (the existing
metric lexicon) are all inapplicable for the company's sector per `sector_applicability.yaml`,
with `abstain_reason="not_applicable_sector"`. A question naming at least one applicable metric
(plain "margins" includes net margin) still searches. `scripts/build_golden.py` relabels the
gross-margin templates of a company with no gross margin as must-abstain, in place, so question
ids stay stable; the answerable pool goes from 82 to 80 and the abstain pool from 24 to 26.
Thresholds are unchanged.

## Consequences

Bank gross-margin questions are declined consistently in every phrasing, and the eval no longer
rewards an answer that DR-042 forbids. The decline message is still the generic "couldn't find
evidence" one; a sector-specific message ("gross margin does not apply to banks") would be
friendlier and is the natural follow-up. Rates published before 2026-11-20 used the 82/24 split.
