# ADR-0064: Alert emails go out from a committed outbox; price alerts check every new bar

- **Status:** Accepted (amends ADR-0039)
- **Date:** 2026-11-17
- **Spec refs:** §26.2, FR-AL-003, FR-AL-005, FR-AL-007, FR-AL-018, CIT-006

## Problem

Three alert leftovers from Phase 7a. (1) Dispatch sent an alert email inside its transaction, so
a crash before the job's final commit rolled the notifications back and the next run emailed the
whole batch again; an SMTP error also rolled back the in-app row. (2) `price_move` compared only
each company's two newest bars, so when a run caught up several missed days a large move on an
earlier day was never checked. Every price run re-fetches years of bars and the upsert reset
`ingested_at` on all of them, so "bars stored in the window" could not tell new days from
history. (3) `filing:<id>` evidence IDs (alerts, earnings briefs) did not resolve through
`GET /sources/{id}`, so their citation chips opened "source not found".

## Decision

(1) `notifications.emailed_at` makes the notifications table the email outbox. Dispatch only
creates rows; the `send_alerts` task commits them, then `send_pending_emails` claims each unsent
email row with `FOR UPDATE SKIP LOCKED`, sends it, sets `emailed_at` and commits that row alone. A
failed send is logged and left pending for the next run. Rows emailed before the migration are
marked sent by it. (2) The price upsert no longer touches `ingested_at`, which now means "first
stored". `price_move` checks every bar first stored inside the window and dated after the newest
bar known before it, each against the bar before it by date; a company with no earlier bars (its
first load of years of history) counts only its newest bar. (3) `filing:<id>` resolves to a
`text_chunk` naming the filing (ticker, form, filed date, period, 8-K items) with the filing on
EDGAR as the deep link, as red-flag IDs already do.

## Consequences

A crash now repeats at most one email (sent, then killed before its own commit), and an SMTP
outage delays email instead of dropping the in-app notification. Emails trail their in-app row by
the length of one dispatch run. A bar the provider later revises keeps its first `ingested_at`,
so a revised close does not re-fire. Backfilling older dates (raising `BACKFILL_PRICE_YEARS`)
never alerts. A filing has no passage to highlight, so its source shows a one-line description.
