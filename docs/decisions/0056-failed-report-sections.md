# ADR-0056: A report whose model calls all fail is not saved

- **Status:** Accepted
- **Date:** 2026-11-05
- **Spec refs:** §17.2, ERR-002, FR-030, HAL-001

## Problem

`generate_section` caught every model error and returned an empty section, the same as a section
with no citable evidence. When Gemini refused every call on 2026-11-03 (prepaid credit gone, HTTP
402), the report was still saved as `complete` with every section "insufficient", and became the
company's latest report in place of a good one (reports 37 and 38, marked `failed` by hand).

## Decision

A section whose model call raised on its last attempt, or whose generation raised at all, is
marked `failed` (`SectionGeneration.failed`). A reply in the wrong shape, or one with nothing
citable, is not a failure: abstaining stays a valid outcome (HAL-001). When every section failed,
`generate_report` raises `ReportGenerationError` before anything is persisted, so the job retries
and then dead-letters, and `GET /companies/{ticker}/research/latest` keeps serving the previous
report.

## Alternatives considered

- **Fail on any failed section:** a section that fails every time (malformed JSON from the model)
  would then block every report for that company, and each retry pays for the other sections again.
- **Save the report with a `partial` status:** changes the report contract and the UI; left for the
  partial case below.

## Consequences

A report with only some sections failed is still saved as `complete`, and those sections read as
"insufficient evidence" (report 36 lost six of twelve). Naming them as failures, and keeping or
merging the previous report, is open in `Instructions/TODO.md`.
