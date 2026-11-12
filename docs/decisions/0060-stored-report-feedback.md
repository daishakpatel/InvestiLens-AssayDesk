# ADR-0060: Report feedback names its report and is stored once per reader

- **Status:** Accepted
- **Date:** 2026-11-11
- **Spec refs:** §10.16, §18.5 (human feedback loop), §24.2 `POST /feedback/report`

## Problem

`POST /feedback/report` accepted `{rating, reason}` and returned 204 without storing anything,
because the body did not say which report it was about. The `report_feedback` table (report_id,
claim_id, rating, reason, user_id) already existed and was empty.

## Decision

The request body gains `report_id` (required) and `claim_id` (optional; must be a claim of that
report). The endpoint requires sign-in, because `user_id` is not nullable and an anonymous rating
can't be tied to a reader or deduplicated. `rating` is stored as +1 (up) or −1 (down) in the
existing integer column. A reader has one row per report, or per claim: rating again replaces the
earlier rating and reason instead of adding rows, so counts reflect readers, not clicks. `reason` is
trimmed and capped at 2,000 characters, and the cap also applies to chat feedback, which shares the
request model. The report page offers a whole-report thumbs up/down to signed-in readers. Claim
ratings are API-only for now, to keep every claim free of controls.

## Consequences

Breaking only on paper: no client sent the old body, and the old endpoint stored nothing. The
§18.5 triage into the golden set is still manual: rows are queryable but there is no admin view
yet (the rejected-claims view in TODO.md is the natural home). Signed-out readers can't rate.
