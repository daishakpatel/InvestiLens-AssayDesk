import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { cashEarningsRows } from "../charts/CashEarningsChart";
import { QualityCard } from "../components/quality/QualityCard";
import type { QualityMeasure, QualityPoint } from "../types";
import { bandLabel, bandTone, formatQuality, missingText, peerText, statusText } from "./earningsQuality";

// FR-EQ-011..014, DR-EQ-001/002: every state renders in words; colour only for "above threshold".

const CAVEAT =
  "Screening heuristic. Calibrated on US non-financial companies. Not a conclusion about this company's accounting.";

function point(overrides: Partial<QualityPoint> = {}): QualityPoint {
  return {
    period: "FY2026",
    fiscal_year: 2026,
    value: "-1.16939208",
    status: "ok",
    band: "above_threshold",
    reason: null,
    reason_text: null,
    missing: [],
    source_id: "derived:NVDA:beneish_m:FY2026",
    formula_id: "beneish_m_score",
    inputs: [],
    details: {},
    ...overrides,
  };
}

function measure(name: string, p: QualityPoint, extra: Partial<QualityMeasure> = {}): QualityMeasure {
  return {
    metric_name: name,
    label: name === "beneish_m" ? "Beneish M-score" : name,
    unit: "score",
    explanation: "Eight-ratio model.",
    caveat: name === "beneish_m" || name === "altman_z" ? CAVEAT : null,
    points: [p],
    latest_ttm: null,
    peer_percentile: null,
    peer_count: 0,
    ...extra,
  };
}

const CAVEAT_HTML = CAVEAT.replace("'", "&#x27;"); // as React escapes it in markup

const render = (lead: QualityMeasure, companion?: QualityMeasure) =>
  renderToStaticMarkup(createElement(QualityCard, { title: "Card", lead, companion }));

describe("formatQuality", () => {
  it("prints each measure in its own unit and never prints zero for missing", () => {
    expect(formatQuality("accruals_ratio", "0.1090")).toBe("10.9%");
    expect(formatQuality("dilution", "-0.0117")).toBe("−1.2%");
    expect(formatQuality("dso", "52.003")).toBe("52 days");
    expect(formatQuality("fcf_conversion", "0.8052")).toBe("0.81×");
    expect(formatQuality("piotroski_f", "4.00000000")).toBe("4 / 9");
    expect(formatQuality("beneish_m", "-1.16939208")).toBe("−1.17");
    expect(formatQuality("beneish_m", null)).toBe("—");
  });
});

describe("bands and colour", () => {
  it("uses the warning tone only above the Beneish threshold (FR-EQ-012)", () => {
    expect(bandTone("beneish_m", "above_threshold")).toBe("warning");
    expect(bandTone("beneish_m", "below_threshold")).toBe("neutral");
    expect(bandTone("altman_z", "distress")).toBe("neutral");
    expect(bandLabel("grey")).toBe("Grey zone");
    expect(bandLabel(null)).toBeNull();
  });
});

describe("status text (DR-EQ-002)", () => {
  it("names the missing inputs and explains not applicable", () => {
    const m = measure("beneish_m", point());
    expect(statusText(point(), m)).toBeNull();
    expect(missingText(["prior_sga_expense", "total_liabilities", "dsri: zero base"])).toBe(
      "prior-year sg&a expense, total liabilities, DSRI has a zero base",
    );
    const na = point({ status: "not_applicable", value: null, reason_text: "Not computed for banks." });
    expect(statusText(na, m)).toBe("Not computed for banks.");
    const partial = point({ status: "insufficient_data", value: null, details: { computable: 8 } });
    expect(statusText(partial, measure("piotroski_f", partial))).toBe(
      "8 of 9 tests computable; no total without all nine.",
    );
    const negative = point({ status: "insufficient_data", value: null, reason: "NEGATIVE_BASE" });
    expect(statusText(negative, m)).toBe("Not meaningful: the base is zero or negative.");
    expect(statusText(undefined, m)).toBe("No fiscal year reported yet.");
  });

  it("hides peer context below three peers (FR-EQ-010)", () => {
    expect(peerText(measure("dso", point()))).toBeNull();
    expect(peerText(measure("dso", point(), { peer_percentile: "0.75", peer_count: 3 }))).toBe(
      "75th percentile vs 3 peers (latest fiscal year)",
    );
    // ADR-0070: the calendar year the peers were compared in, when the API says which.
    const aligned = { peer_percentile: "0.75", peer_count: 3, peer_calendar_year: 2025 };
    expect(peerText(measure("dso", point(), aligned))).toBe("75th percentile vs 3 peers (calendar 2025)");
    expect(peerText(measure("dso", point(), { peer_percentile: "0.22", peer_count: 3 }))).toMatch(/^22nd/);
  });
});

describe("QualityCard renders every state (FR-EQ-011/013)", () => {
  it("ok: the value, a labelled warning band, and the caveat strip", () => {
    const html = render(measure("beneish_m", point()));
    expect(html).toContain("−1.17");
    expect(html).toContain("Above −1.78 threshold");
    expect(html).toContain("bg-warning");
    expect(html).toContain(CAVEAT_HTML);
    expect(html).not.toMatch(/bg-(positive|negative)|text-(positive|negative)/);
    expect(html).not.toMatch(/manipulat|fraud/i);
  });

  it("insufficient data: no number, the missing input named", () => {
    const p = point({ status: "insufficient_data", value: null, band: "insufficient_data", missing: ["total_liabilities"] });
    const html = render(measure("altman_z", p));
    expect(html).toContain('data-status="insufficient_data"');
    expect(html).toContain("Insufficient data: missing total liabilities.");
    expect(html).not.toContain("—");
    expect(html).toContain(CAVEAT_HTML);
  });

  it("not applicable: says why, shows no band", () => {
    const p = point({ status: "not_applicable", value: null, band: null, reason_text: "Not computed for banks." });
    const html = render(measure("beneish_m", p));
    expect(html).toContain('data-status="not_applicable"');
    expect(html).toContain("N/A. Not computed for banks.");
    expect(html).not.toContain("threshold</span>");
  });

  it("shows the Altman model variant and a companion measure", () => {
    const altman = point({ value: "59.93", band: "safe", details: { model_variant: "original" } });
    const piotroski = point({ value: "4", band: null });
    const html = render(measure("altman_z", altman), measure("piotroski_f", piotroski));
    expect(html).toContain("Safe zone");
    expect(html).toContain("Original Z (manufacturer)");
    expect(html).toContain("4 / 9");
  });
});

describe("cash vs earnings rows (FR-EQ-014)", () => {
  it("reads net income, cash flow and FCF from the measures' own inputs", () => {
    const accruals = measure(
      "accruals_ratio",
      point({
        value: "0.109",
        inputs: [
          { name: "net_income", value: "120067000000", source_id: "xbrl:NVDA:net_income:FY2026" },
          { name: "operating_cash_flow", value: "102718000000", source_id: "x" },
        ],
      }),
    );
    const conversion = measure(
      "fcf_conversion",
      point({ inputs: [{ name: "free_cash_flow", value: "96676000000", source_id: "y" }] }),
    );
    expect(cashEarningsRows(accruals, conversion)).toEqual([
      {
        period: "FY2026",
        label: "FY26",
        netIncome: 120067000000,
        operatingCashFlow: 102718000000,
        freeCashFlow: 96676000000,
        accruals: 0.109,
      },
    ]);
    expect(cashEarningsRows(undefined, undefined)).toEqual([]);
  });
});
