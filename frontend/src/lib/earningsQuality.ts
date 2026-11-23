// Display rules for the earnings-quality tab (Phase 7d). Values arrive computed (DR-040); this only
// decides how to print them and what a status or band says in words. Colour rule (FR-EQ-012):
// green/red are for gain/loss only, so a score above a threshold is `warning`, anything else neutral.
import { toNumber } from "./format";
import { metricLabel } from "./metricCatalog";
import type { QualityMeasure, QualityPoint } from "../types";

export type Tone = "warning" | "neutral";

const PERCENT = new Set(["accruals_ratio", "accruals_ratio_bs", "dilution"]);
const DAYS = new Set(["dso", "dio", "dpo", "cash_conversion_cycle"]);

/** "10.9%", "0.81×", "52 days", "−1.17", "4 / 9". Never a zero for a missing value. */
export function formatQuality(metric: string, value: string | null | undefined): string {
  const n = toNumber(value);
  if (n === null) return "—";
  const sign = n < 0 ? "−" : "";
  const size = Math.abs(n);
  if (PERCENT.has(metric)) return `${sign}${(size * 100).toFixed(1)}%`;
  if (DAYS.has(metric)) return `${sign}${size.toFixed(0)} days`;
  if (metric === "fcf_conversion") return `${sign}${size.toFixed(2)}×`;
  if (metric === "piotroski_f") return `${size.toFixed(0)} / 9`;
  return `${sign}${size.toFixed(2)}`;
}

const BANDS: Record<string, string> = {
  above_threshold: "Above −1.78 threshold",
  below_threshold: "Below −1.78 threshold",
  distress: "Distress zone",
  grey: "Grey zone",
  safe: "Safe zone",
};

export function bandLabel(band: string | null | undefined): string | null {
  return band ? (BANDS[band] ?? null) : null;
}

/** Only "above the Beneish threshold" earns a colour, and it is the warning hue (FR-EQ-012). */
export function bandTone(metric: string, band: string | null | undefined): Tone {
  return metric === "beneish_m" && band === "above_threshold" ? "warning" : "neutral";
}

/** The input names behind a gap, in words: "prior-year SG&A expense, total liabilities". */
export function missingText(missing: readonly string[]): string {
  return missing
    .map((name) => {
      if (name.includes(": zero base")) return `${name.split(":")[0]?.toUpperCase()} has a zero base`;
      const prior = name.startsWith("prior_");
      const label = metricLabel(prior ? name.slice("prior_".length) : name).toLowerCase();
      return prior ? `prior-year ${label}` : label;
    })
    .join(", ");
}

/** Why a point has no value, as one sentence for the card. Null when it has one. */
export function statusText(point: QualityPoint | undefined, measure: QualityMeasure): string | null {
  if (!point) return "No fiscal year reported yet.";
  if (point.status === "ok") return null;
  if (point.status === "not_applicable") return point.reason_text ?? "Not applicable to this company.";
  if (measure.metric_name === "piotroski_f") {
    const computable = Number(point.details["computable"] ?? 0);
    return `${computable} of 9 tests computable; no total without all nine.`;
  }
  if (point.reason === "NEGATIVE_BASE" && point.missing.length === 0) {
    return "Not meaningful: the base is zero or negative.";
  }
  return point.missing.length > 0
    ? `Insufficient data: missing ${missingText(point.missing)}.`
    : "Insufficient data.";
}

function ordinal(n: number): string {
  const teen = n % 100 >= 11 && n % 100 <= 13;
  const suffix = teen ? "th" : ({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[n % 10];
  return `${n}${suffix ?? "th"}`;
}

/** "75th percentile vs 3 peers (calendar 2025)", or null when hidden (fewer than three peers,
 * FR-EQ-010). Peers are compared in one calendar year, so NVIDIA's January FY2026 is 2025. */
export function peerText(measure: QualityMeasure): string | null {
  const pct = toNumber(measure.peer_percentile);
  if (pct === null) return null;
  const year = measure.peer_calendar_year ? `calendar ${measure.peer_calendar_year}` : "latest fiscal year";
  return `${ordinal(Math.round(pct * 100))} percentile vs ${measure.peer_count} peers (${year})`;
}

export function latestPoint(measure: QualityMeasure | undefined): QualityPoint | undefined {
  return measure?.points.at(-1);
}
