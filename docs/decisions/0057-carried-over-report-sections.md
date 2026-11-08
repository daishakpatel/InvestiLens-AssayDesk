# ADR-0057: A section whose model call failed is carried over from the previous report

- **Status:** Accepted
- **Date:** 2026-11-07
- **Spec refs:** §17.2, ERR-002, FR-030, HAL-001, CIT-001

## Problem

ADR-0056 stopped a report whose model calls *all* fail from being saved, but a report with only some
sections failed was still saved as `complete`, with those sections listed in
`insufficient_evidence_sections`. On 2026-11-03 report 36 lost six of twelve sections to the 402s
and became the company's latest report: the reader saw six sections "not covered for lack of
evidence" when the evidence was there and the previous report had written them.

## Decision

`ResearchReport` gains `failed_sections` and `carried_over` (`research_id`, `generated_at`,
`sections`). A section whose model call failed is no longer listed as insufficient. Instead, when
the latest complete report for the company wrote that section, its content is copied unchanged
into the new report, the section is named in `carried_over` with that report's id and date, and
its stored sources are copied too (CIT-001). A failed section with nothing to copy is listed in
`failed_sections`. A section the previous report itself carried over is not copied again, so the
date shown is always the date its content was written. Carried sources join the evidence set
behind `data_version`. The report page says which sections are from which date, and names failed
sections apart from the ones with too little evidence.

## Consequences

A partial outage no longer erases good sections, and the reader is told what is stale. The cost is
mixed provenance: a carried section was written against an older data set and prompt version than
the rest of the report. Two outages in a row lose a carried section (it shows as failed), which
keeps dates honest at the cost of content; tracking each section's own origin would fix that and
was not worth a per-section provenance model yet. Reports saved before this change have neither
field and read as before.
