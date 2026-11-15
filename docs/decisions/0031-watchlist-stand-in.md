# ADR-0031: Watching a company is stored as a one-company watchlist, until the API can add items

- **Status:** Superseded by ADR-0062
- **Date:** 2026-10-11
- **Spec refs:** §26.2, FR-007; UI plan Step 5

## Problem

The watchlist API can list, create and delete a watchlist (`GET`/`POST /watchlists`,
`DELETE /watchlists/{id}`), and a watchlist is returned with its `items`, but no operation adds
a company to one or removes it. UI plan Step 5 requires a Watch button and a watchlist on Home,
and forbids backend changes.

## Decision

Until the API gains that operation, the frontend stores each watched company as its own
watchlist whose `name` is the ticker. Watch creates it; unwatch deletes it. The convention lives
in one file, `frontend/src/lib/watchlist.ts`: `watchedTickers` reads these lists and also the
`items` of any real list, and `watchEntry` finds the list to delete. Nothing else knows about it.

## Consequences

Watching works and persists per user with no backend change. The data is not what the model
intends: a user with ten watched companies has ten watchlists, none with items, and a list named
by something that is not a ticker is ignored by the UI. When the API gains
`POST`/`DELETE /watchlists/{id}/items`, change the two calls in `WatchButton` and `watchEntry`,
and migrate existing one-company lists into a default list (a small backend data migration).
Related gaps found at the same time: an alert cannot carry a threshold or a news category
through the API (the dispatcher reads them from `alert.config`, which `POST /alerts` does not
accept), and a notification names its company only by internal id.
