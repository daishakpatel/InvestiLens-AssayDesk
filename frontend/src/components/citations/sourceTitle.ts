import { metricLabel } from "../../lib/metricCatalog";
import type { SourceRecord } from "../../types";

/** A human label for a source record, by type (the evidence panel and the reference list). */
export function sourceTitle(source: SourceRecord): string {
  switch (source.source_type) {
    case "text_chunk":
      return source.section_path.length ? source.section_path.join(" › ") : "Filing excerpt";
    case "table_chunk":
      return `Table ${source.table_id}`;
    case "xbrl_fact":
      return `${source.concept_tag} (${source.accession_number})`;
    case "derived_metric":
      return `${metricLabel(source.formula_id)}${source.period ? ` · ${source.period}` : ""}`;
    case "news_item":
      return source.publisher ? `${source.publisher} article` : "News article";
    case "price_bar":
      return `${source.ticker} share price · ${source.end_date ? `${source.date} to ${source.end_date}` : source.date}`;
    case "earnings_release":
    case "transcript":
      return source.source_type === "transcript" ? "Earnings call transcript" : "Earnings release";
    default:
      return "Source";
  }
}

export function sourceKindLabel(source: SourceRecord): string {
  const map: Record<SourceRecord["source_type"], string> = {
    text_chunk: "Filing text",
    table_chunk: "Filing table",
    xbrl_fact: "Reported figure (XBRL)",
    derived_metric: "Calculated metric",
    news_item: "News",
    price_bar: "Market data",
    earnings_release: "Earnings release",
    transcript: "Transcript",
  };
  return map[source.source_type];
}

// The source-quality hierarchy (§15): lower is closer to the primary record.
const TIERS: Record<number, string> = {
  1: "SEC filing",
  2: "Company earnings release",
  3: "Investor-relations material",
  4: "Licensed news or market data",
  5: "Other source",
};

export function tierLabel(tier: number | null | undefined): string | null {
  return tier != null && TIERS[tier] ? `Tier ${tier} · ${TIERS[tier]}` : null;
}
