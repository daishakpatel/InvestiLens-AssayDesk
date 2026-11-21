# ADR-0066: Chat growth comparisons come from stored figures

- **Status:** Accepted
- **Date:** 2026-11-21
- **Spec refs:** §16.1, RAG-030, CIT-003, CIT-006, ADR-0055

## Problem

"Did NVIDIA's revenue grow faster than its net income last year?" went to document search: the
metric router only knew one metric per question, and only revenue had a growth figure (the
report's `derived:<T>:revenue_yoy:…` ID). The live model, which may not compute numbers, declined
it for NVIDIA and Apple (two of the seven live false abstentions on 2026-11-20).

## Decision

`run_retrieval` reads every metric a question names (`_all_metrics`, a matched phrase consumed so
"earnings per share" is not also "earnings"). Two or more flows (margins excluded) plus a growth
word and no "why" route to `_growth_answer`: `calculate_growth` from FY<end-1> to FY<end> (the
stated year, else the latest), each cited `derived:<T>:<metric>_yoy:FY<end-1>-FY<end>`. The
resolver's growth pattern is generalized from revenue to any stored FY metric; revenue keeps its
`yoy_growth` label, others read "Net income growth" / "Diluted EPS growth". Nothing new is stored.

## Consequences

Growth comparisons are deterministic, cited and free (no model call). The answer states each
growth rate but does not say "faster" in words; the reader compares two cited figures. Quarterly
or multi-year comparisons still go to documents; widen the route if users ask them.
