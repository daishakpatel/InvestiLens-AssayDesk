# ADR-0059: Report diff matches claims by evidence; export stays in the browser

- **Status:** Accepted
- **Date:** 2026-11-10
- **Spec refs:** §10.16 (versioning, "what changed since last report", export), §24.2, Appendix F #9

## Problem

`GET /research/{id}/diff` and `GET /research/{id}/export` were 501 stubs. Two reports of one
company are never worded the same, because the model rewrites every claim on each run, so a text
diff would mark everything as changed. Export already works without the server:
`frontend/src/lib/reportExport.ts` writes Markdown with the citation list and the disclaimer
(LGL-001), and PDF goes through the print stylesheet. Spec Appendix F #9 left "server render vs
client print" open.

## Decision

**Diff** is deterministic and makes no model call. `against` is optional and defaults to the
company's newest completed report created before this one. It is 404 when there is no such report,
or when `against` belongs to another company. Claims are compared within each section by evidence:
a claim is unchanged when it shares at least one cited source with a claim in the same section of
the other report. Sections that failed in either report (ADR-0056) are listed as skipped instead
of shown as dropped. Metrics: for each (metric, period type) cited, the latest period each report
cites, with values from the stored `financial_metrics` rows. A row appears when that period
differs, or when only one report cites the metric. The report page shows the result as a "What
changed" section.

**Export** stays client-side, and the stub endpoint is removed from the contract. Markdown
download and browser print already meet §10.16 and LGL-001, and a server PDF renderer would add a
dependency to produce the same output.

## Consequences

On real reports written minutes apart, shared-source matching still marks about a quarter to a
third of claims new (2026-11-10: JPM 26 of 76, AAPL 23 of 77, INTC 5 of 103), mostly news items,
which are actually different. Exact-source-set matching marked 21–44. A claim that kept one old
source and gained newer ones counts as unchanged; the metric rows show the newer figures. Values
are the current stored rows, not a snapshot taken at report time, so a restatement since the
earlier report shows its restated value. With the endpoint removed, the RFC 7807 contract test now
uses a routing 404. A server PDF can be added later if a shareable link (§10.16) needs one.
