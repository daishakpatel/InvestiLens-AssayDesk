// Which companies the signed-in user is watching: every company on any of their lists
// (ADR-0062; the ADR-0031 one-list-per-ticker stand-in was merged into items by a migration).
import type { Watchlist } from "../types";

/** Watched tickers, in list order, without repeats. */
export function watchedTickers(lists: readonly Watchlist[]): string[] {
  return [...new Set(lists.flatMap((list) => list.items.map((item) => item.ticker.toUpperCase())))];
}
