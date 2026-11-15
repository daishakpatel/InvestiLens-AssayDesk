import { Star } from "lucide-react";

import { useAuth } from "../../lib/auth";
import { useSetWatched, useWatchlists } from "../../lib/queries";
import { watchedTickers } from "../../lib/watchlist";
import { SignInHint } from "../SignInHint";
import { Popover } from "../ui/Popover";

const ACTION =
  "inline-flex h-9 items-center gap-2 rounded-control border border-border-strong px-3.5 text-sm " +
  "font-medium transition-colors hover:bg-surface-2 active:bg-surface-3 disabled:opacity-60";

/** Adds the company to, or removes it from, the reader's watchlist. The button's own state is
 *  the confirmation: no toast. */
export function WatchButton({ ticker }: { ticker: string }) {
  const { user } = useAuth();
  const lists = useWatchlists(user?.id ?? null);
  const setWatched = useSetWatched();
  const symbol = ticker.toUpperCase();

  if (!user) {
    return (
      <Popover
        align="end"
        trigger={
          <button type="button" className={ACTION}>
            <Star aria-hidden className="size-4" />
            Watch
          </button>
        }
      >
        <SignInHint>Sign in to keep a watchlist. Watched companies appear on your home page.</SignInHint>
      </Popover>
    );
  }

  const watched = watchedTickers(lists.data ?? []).includes(symbol);
  const busy = lists.isPending || setWatched.isPending;
  const failed = lists.isError || setWatched.isError;

  return (
    <span className="relative">
      <button
        type="button"
        aria-pressed={watched}
        disabled={busy}
        onClick={() => setWatched.mutate({ lists: lists.data ?? [], ticker: symbol, watch: !watched })}
        className={`${ACTION} ${watched ? "bg-surface-2" : ""}`}
      >
        <Star aria-hidden className="size-4" fill={watched ? "currentColor" : "none"} />
        {watched ? "Watching" : "Watch"}
      </button>
      {failed && (
        <span role="alert" className="absolute right-0 top-full mt-1 whitespace-nowrap text-caption text-negative">
          Could not update your watchlist. Try again.
        </span>
      )}
    </span>
  );
}
