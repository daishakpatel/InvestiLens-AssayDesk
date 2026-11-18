# ADR-0039: Per-alert watermark plus unique (alert_id, event_key) for alert idempotency

- **Status:** Accepted; its email-before-commit gap is closed by ADR-0064
- **Date:** 2026-10-21
- **Spec refs:** §26.2, §37.4, FR-AL-001 … FR-AL-018 (Appendix H)

## Problem

Phase 5d's dispatch used `last_triggered_at`, falling back to the epoch for an alert that had
never fired, so a new alert replayed the newest matching old event (an 8-K filed last month).
The watermark moved only when something fired, so a crash between "notification written" and
"watermark advanced", or two overlapping Celery runs, could deliver an event twice. Phase 7 adds
five more event types, and each would otherwise bring its own query, dedupe and failure modes.

## Decision

Alerts become typed rules over an event-type registry (`app/alerts/registry.py`): each type has a
Pydantic params model and a deterministic detector returning events with a stable `event_key`.
Two independent guards make delivery exactly-once per (alert, event): (1) a per-alert
`watermark_at` — dispatch only considers `watermark_at < occurred_at <= run_started_at`, then
sets the watermark to the run's *start* so events landing mid-run are not lost; new alerts start
at `created_at`, and the migration backfilled existing rows with `now()`, not their age; (2) a
unique `notifications(alert_id, event_key)` constraint with `INSERT … ON CONFLICT DO NOTHING`,
emailing only rows the insert actually created. The watermark keeps each run's work small; the
constraint makes correctness independent of it. More than 20 events for one alert in one run
become one digest whose payload lists every `event_key`. The alert API changes shape
(`alert_type`/`channel`/`is_active` → `event_type`/`params`/`channels`/`enabled`, ticker optional
for watchlist-wide alerts); the only client is our frontend, updated in the same change.

## Consequences

A new event type is one `register(...)` call. Retired types stay in the table and are skipped
with a warning, shown to the user as "no longer supported". Costs: a JSONB `payload` and an
indexed `event_key` per notification; detectors must produce truly deterministic keys (a key
that varies per run would defeat the constraint). Edge case accepted: if a digest is retried
after new events arrived, only the new events are sent (already-delivered keys are filtered
first), so nothing repeats, but the second digest is smaller than the first. Email is sent before
the job's final commit, so a crash in that window can still resend an email (the in-app row is
the durable record); an outbox table would close that gap if email becomes a primary channel.
