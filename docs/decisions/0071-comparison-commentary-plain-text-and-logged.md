# ADR-0071: Comparison commentary is verified as plain sentences and logged

- **Status:** Accepted
- **Date:** 2026-11-22
- **Spec refs:** §37.1, CIT-005, LGL-006, OBS-005, ADR-0022

## Problem

Comparison commentary first ran on a real model on 2026-11-22. The model answered in Markdown
(headings, bullets, bold). The verifier splits text into sentences on punctuation, so a bulleted
answer of thirty figures became one or two "claims": per-sentence checking did not happen, and
whether the one giant claim passed was luck (NVDA/AMD/INTC came back empty on one run, wholly
accepted on the next, with uncited figures in it). The call was also never written to `llm_calls`,
so its cost never reached the caller's AI budget even though the endpoint checks that budget.

## Decision

The commentary prompt asks for plain sentences without Markdown, like chat's, and the model's
answer goes through `app/citation/markdown.plain_text` (moved from the chat service, which now
imports it) before `verify_text`. `generate_commentary` records the call as
`purpose="comparison_commentary"` with the endpoint's `user_id`, and the endpoint commits.

## Consequences

Live, NVDA/AMD/INTC now yields 16 claims (13 accepted) and AAPL/NVDA/JPM 12 (8 accepted); every
figure is cited, all 32 and 29 citations resolve, and uncited framing sentences are dropped.
Filing-diff adjudication, the other never-checked model call, had already run live inside report
generation (127 calls); a spot check of 15 borderline pairs found every model verdict ("same
passage, reworded") correct, with the per-diff cap of 8 sending the rest to the threshold, which
agreed. A live "different passage" verdict has not been observed yet.
