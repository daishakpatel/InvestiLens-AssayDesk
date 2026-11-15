import { describe, expect, it } from "vitest";

import type { Watchlist } from "../types";
import { watchedTickers } from "./watchlist";

const list = (id: string, name: string, items: string[] = []): Watchlist => ({
  id,
  name,
  items: items.map((ticker) => ({ ticker })),
});

// FR-007 (watchlist), ADR-0062.
describe("watchedTickers", () => {
  it("reads every list's items, in order, upper-cased, without repeats", () => {
    const lists = [list("1", "Watchlist", ["nvda", "AMD"]), list("2", "Chips", ["AMD", "INTC"])];
    expect(watchedTickers(lists)).toEqual(["NVDA", "AMD", "INTC"]);
  });

  it("ignores a list's name: an empty list named like a ticker watches nothing", () => {
    expect(watchedTickers([list("1", "NVDA")])).toEqual([]);
  });
});
