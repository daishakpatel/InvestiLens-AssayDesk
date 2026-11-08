import { describe, expect, it } from "vitest";

import { buildNumbering } from "./reportCitations";
import type { Factor, ResearchClaim, ResearchReport, Risk } from "../types";

const claim = (text: string, sourceIds: string[]): ResearchClaim => ({
  claim_id: text,
  text,
  source_ids: sourceIds,
});

const emptyReport: ResearchReport = {
  executive_summary: [],
  company_overview: [],
  revenue_analysis: [],
  profitability_analysis: [],
  balance_sheet_analysis: [],
  cash_flow_analysis: [],
  valuation_analysis: [],
  news_summary: [],
  risks: [],
  management_commentary: [],
  bull_factors: [],
  bear_factors: [],
  insufficient_evidence_sections: [],
  failed_sections: [],
};

describe("buildNumbering (CIT-006 first-appearance ordering)", () => {
  it("numbers unique source ids in appearance order across all sections", () => {
    const risk: Risk = { category: "supply_chain", description: "r", source_ids: ["s2", "s3"] };
    const factor: Factor = {
      title: "Upside",
      claims: [claim("c", ["s3", "s4"])],
      counterpoint_source_ids: [],
    };
    const report: ResearchReport = {
      ...emptyReport,
      executive_summary: [claim("a", ["s1", "s2"])],
      risks: [risk],
      bull_factors: [factor],
    };

    const { order, numberOf } = buildNumbering(report);
    expect(order).toEqual(["s1", "s2", "s3", "s4"]); // de-duplicated, in first-appearance order
    expect(numberOf("s1")).toBe(1);
    expect(numberOf("s3")).toBe(3);
    expect(numberOf("unknown")).toBe(0); // unmapped id → 0, never throws
  });

  it("returns an empty numbering for an empty report", () => {
    const { order, numberOf } = buildNumbering(emptyReport);
    expect(order).toEqual([]);
    expect(numberOf("x")).toBe(0);
  });
});
