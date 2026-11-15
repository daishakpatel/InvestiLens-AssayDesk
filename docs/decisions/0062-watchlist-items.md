# ADR-0062: Watching a company adds an item to a list; the one-list-per-ticker stand-in is merged away

- **Status:** Accepted (supersedes ADR-0031)
- **Date:** 2026-11-15
- **Spec refs:** §26.2, FR-007, FR-AL-011, FR-RF-009, FR-EB-012, SEC-007

## Problem

The API could create and delete a watchlist but not add a company to one, so ADR-0031 stored each
watched company as its own empty list named by its ticker. Everything server-side that reads "the
user's watchlist" (watchlist-wide alerts, `GET /red-flags?watchlist=true`, earnings-brief
eligibility) reads `watchlist_items`, so for every real user it saw an empty watchlist.

## Decision

`POST /watchlists/{id}/items` (`{ticker}`) adds a company to a list the caller owns and returns
the list; `DELETE /watchlists/{id}/items/{ticker}` removes it. Another user's list is 404, an
unknown ticker on add is 404, and both are idempotent: `watchlist_items` gets a unique
`(watchlist_id, company_id)` and the insert is `ON CONFLICT DO NOTHING`, so a double click or two
tabs never make a duplicate. Migration `c3e8a1f5d7b2` turns each user's stand-ins into items on
one list (their oldest stand-in, renamed "Watchlist", keeping the watch order) and deletes the
rest; lists with items or a non-ticker name are untouched. The Watch button adds to the user's
first list, creating "Watchlist" when they have none, and Unwatch removes the company from every
list holding it. The frontend no longer reads list names as tickers.

## Consequences

Watched companies now drive alerts, the red-flag feed and briefs. The UI still shows one merged
watchlist and offers no list management (rename, several lists); the API already supports that.
The migration's merge is not reversed on downgrade (the stand-ins held nothing beyond the ticker,
which the merged list keeps). A user with no list who clicks Watch in two tabs at once can end up
with two lists, both read as one watchlist; harmless, so not guarded.
