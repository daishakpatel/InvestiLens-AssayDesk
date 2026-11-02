# ADR-0054: Stock prices are a citable source, and price questions are answered from them

- **Status:** Accepted
- **Date:** 2026-11-01
- **Spec refs:** §13.3, §15, §16.2, RAG-002, RAG-030, RAG-031, CIT-006

## Problem

Chat could not answer "what was the stock high on Sep 29?" or "stock price today": prices had no
source type, so no answer could cite one. Such a question either abstained (bare "price" made it
a metric lookup that `financial_metrics` cannot answer) or went to document search, where one sent
23k tokens of unrelated filing text against a 6,000-token budget. The `price:` IDs that alerts,
earnings reactions and valuations already issue did not resolve through `GET /sources/{id}`.
Plain "margins" was in no lexicon, so "how did margins trend?" also went to the filings.

## Decision

A new `price_bar` member of the `SourceRecord` union (`ticker`, `date`, optional `end_date`,
`provider`) is resolved from `price_history` for `price:<TICKER>:<date>` (one bar: open, high, low,
close, volume) and `price:<TICKER>:<date>:<end_date>` (two closes), exact trading days only. It is
Tier 4, licensed market data, so its text names the provider ("Tiingo end-of-day data"), as §15
requires of Tier 4 claims. Chat recognises a stock-price question (`app/rag/prices.py`, rules
excluding stock-based compensation, buybacks, per-share figures and multiples) before the metric
check, resolves the asked day ("Sep 29" means its latest occurrence on or before the newest bar;
none means the newest bar; an impossible date abstains), and answers deterministically from the
bar on or before that day, like a metric lookup. Plain "margins" asks for gross, operating and net
margin; "trend" with no period means the last five fiscal years; "what did management say about X"
is never a metric lookup. A deterministic structured answer sizes its own tool budget (the
RAG-031 budget of 6 guards a model choosing calls, and a five-year revenue trend needs 9).

## Consequences

Price questions get an exact, cited answer with no model call, and alert and earnings price links
open in the citation drawer. The answer is the stored end-of-day bar, so "today" during trading
hours is the last close, stated with its date. The frontend shows the new type as "Market data";
Tier 4 is now labelled "Licensed news or market data". Intraday prices, 52-week ranges and
returns over a period are not answered yet; revisit with a range-question parser if they are asked.
