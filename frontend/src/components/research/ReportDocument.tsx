import { Download, Printer } from "lucide-react";
import { useState } from "react";
import type { ReactNode } from "react";

import { downloadText, formatDate } from "../../lib/format";
import { buildNumbering } from "../../lib/reportCitations";
import { reportToMarkdown } from "../../lib/reportExport";
import { useActiveSection } from "../../lib/useActiveSection";
import type {
  Factor,
  ManagementTopicStatement,
  NewsItemSummary,
  ResearchClaim,
  ResearchReport,
  ResearchReportEnvelope,
  Risk,
} from "../../types";
import { CitationChips } from "../citations/CitationChip";
import { ReferenceList } from "../citations/ReferenceList";
import { CHANGE_STATUS, isChangeStatus } from "../filings/changeStatus";
import { LanguageTracker } from "../filings/LanguageTracker";
import { RegenerateReport } from "./GenerateReport";
import { AiMark } from "../ui/AiMark";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Popover } from "../ui/Popover";
import { SegmentedControl } from "../ui/SegmentedControl";
import { Select } from "../ui/Select";

type NumberOf = (sourceId: string) => number;
type ProseKey =
  | "executive_summary"
  | "company_overview"
  | "revenue_analysis"
  | "profitability_analysis"
  | "balance_sheet_analysis"
  | "cash_flow_analysis"
  | "valuation_analysis";

const PROSE: { id: string; key: ProseKey; title: string }[] = [
  { id: "summary", key: "executive_summary", title: "Summary" },
  { id: "business", key: "company_overview", title: "The business" },
  { id: "revenue", key: "revenue_analysis", title: "Revenue" },
  { id: "profitability", key: "profitability_analysis", title: "Profitability" },
  { id: "balance-sheet", key: "balance_sheet_analysis", title: "Balance sheet" },
  { id: "cash-flow", key: "cash_flow_analysis", title: "Cash flow" },
  { id: "valuation", key: "valuation_analysis", title: "Valuation" },
];

// Section keys as the API names them, in the reader's words, for the "not covered" list.
const SECTION_NAMES: Record<string, string> = {
  executive_summary: "the summary",
  company_overview: "the business",
  revenue_analysis: "revenue",
  profitability_analysis: "profitability",
  balance_sheet_analysis: "the balance sheet",
  cash_flow_analysis: "cash flow",
  valuation_analysis: "valuation",
  news_summary: "recent news",
  risks: "risks",
  management_commentary: "management commentary",
  bull_factors: "the bull case",
  bear_factors: "the bear case",
};

const words = (key: string) => key.replace(/_/g, " ");
const sentenceCase = (key: string) => words(key).replace(/^./, (c) => c.toUpperCase());

/** No AI claim is shown without a source (FR-020, CIT-001). The backend already drops uncited
 *  claims; this is the same rule applied again at the last step before the reader. */
function cited<T extends { source_ids: string[] }>(items: readonly T[]): T[] {
  return items.filter((item) => item.source_ids.length > 0);
}

function DocSection({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-heading`} className="md:scroll-mt-4">
      <h3 id={`${id}-heading`} className="text-title font-medium">
        {title}
      </h3>
      <div className="mt-4 space-y-4">{children}</div>
    </section>
  );
}

/** A claim, as a paragraph with its citations. Evidence strength (HAL-002) is mentioned only
 *  when it is weak; a well-supported claim needs no badge, and a raw score is never shown. */
function Claim({ claim, numberOf }: { claim: ResearchClaim; numberOf: NumberOf }) {
  return (
    <p className="text-body leading-relaxed">
      {claim.text}
      <CitationChips ids={claim.source_ids} numberOf={numberOf} />
      {claim.confidence_label === "limited_evidence" && (
        <span className="ml-2 align-middle">
          <Badge tone="warning">Limited evidence</Badge>
        </span>
      )}
    </p>
  );
}

function Case({
  title,
  tone,
  factors,
  numberOf,
}: {
  title: string;
  tone: "positive" | "negative";
  factors: Factor[];
  numberOf: NumberOf;
}) {
  return (
    <div className={`border-t-2 pt-4 ${tone === "positive" ? "border-positive" : "border-negative"}`}>
      <h4 className="text-body font-medium">{title}</h4>
      {factors.length === 0 ? (
        <p className="mt-3 text-sm text-muted">Not enough evidence to make this case.</p>
      ) : (
        <div className="mt-3 space-y-5">
          {factors.map((factor) => (
            <div key={factor.title} className="space-y-2">
              <p className="text-sm font-medium">{factor.title}</p>
              {cited(factor.claims).map((claim) => (
                <p key={claim.claim_id} className="text-sm leading-relaxed">
                  {claim.text}
                  <CitationChips ids={claim.source_ids} numberOf={numberOf} />
                </p>
              ))}
              {factor.monitoring_indicator && (
                <p className="text-caption text-subtle">What to watch: {factor.monitoring_indicator}</p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Risks({ risks, numberOf }: { risks: Risk[]; numberOf: NumberOf }) {
  const [filter, setFilter] = useState<"all" | "changed">("all");
  const changed = risks.filter((risk) => risk.change_status === "new" || risk.change_status === "reworded");
  const shown = filter === "changed" ? changed : risks;
  return (
    <>
      {changed.length > 0 && changed.length < risks.length && (
        <SegmentedControl
          label="Which risks to show"
          value={filter}
          onValueChange={setFilter}
          options={[
            { value: "all", label: `All ${risks.length}` },
            { value: "changed", label: `New or reworded (${changed.length})` },
          ]}
        />
      )}
      <ul className="divide-y divide-border/60">
        {shown.map((risk, i) => (
          <li key={`${risk.category}-${i}`} className="space-y-1.5 py-4 first:pt-0">
            <p className="flex flex-wrap items-center gap-x-4 gap-y-1 text-label text-muted">
              <span>{sentenceCase(risk.category)}</span>
              {isChangeStatus(risk.change_status) && (
                <Badge tone={CHANGE_STATUS[risk.change_status].tone}>
                  {CHANGE_STATUS[risk.change_status].inReport}
                </Badge>
              )}
              {risk.evidence_label === "limited_evidence" && <Badge tone="warning">Limited evidence</Badge>}
            </p>
            <p className="text-body leading-relaxed">
              {risk.description}
              <CitationChips ids={risk.source_ids} numberOf={numberOf} />
            </p>
          </li>
        ))}
      </ul>
    </>
  );
}

function Statements({ statements, numberOf }: { statements: ManagementTopicStatement[]; numberOf: NumberOf }) {
  return (
    <ul className="divide-y divide-border/60">
      {statements.map((statement, i) => (
        <li key={`${statement.topic}-${i}`} className="space-y-1.5 py-4 first:pt-0">
          <p className="text-label text-muted">
            {[sentenceCase(statement.topic), statement.period, statement.speaker].filter(Boolean).join(" · ")}
          </p>
          <p className="text-body leading-relaxed">
            {statement.text}
            <CitationChips ids={statement.source_ids} numberOf={numberOf} />
          </p>
        </li>
      ))}
    </ul>
  );
}

function News({ items, numberOf }: { items: NewsItemSummary[]; numberOf: NumberOf }) {
  return (
    <ul className="space-y-4">
      {items.map((item) => (
        <li key={item.news_id} className="space-y-1">
          <p className="text-label text-muted">{sentenceCase(item.category)}</p>
          <p className="text-body leading-relaxed">
            {item.summary}
            <CitationChips ids={item.source_ids} numberOf={numberOf} />
          </p>
        </li>
      ))}
    </ul>
  );
}

interface Entry {
  id: string;
  title: string;
}

function Contents({ entries }: { entries: Entry[] }) {
  const active = useActiveSection(entries.map((entry) => entry.id), 180);
  const jump = (id: string) => document.getElementById(id)?.scrollIntoView();
  return (
    <>
      {/* Wide screens: a table of contents that stays in view and tracks the reader. */}
      <nav aria-label="Report contents" className="sticky top-[7.5rem] hidden self-start lg:block print:hidden">
        <ul className="space-y-0.5 border-l border-border">
          {entries.map((entry) => (
            <li key={entry.id}>
              <a
                href={`#${entry.id}`}
                aria-current={active === entry.id ? "location" : undefined}
                onClick={(event) => {
                  event.preventDefault();
                  jump(entry.id);
                }}
                className={`-ml-px block border-l py-1 pl-4 text-label transition-colors ${
                  active === entry.id
                    ? "border-primary text-text"
                    : "border-transparent text-muted hover:text-text"
                }`}
              >
                {entry.title}
              </a>
            </li>
          ))}
        </ul>
      </nav>
      {/* Narrow screens: the same list as a jump menu. */}
      <div className="mb-8 lg:hidden print:hidden">
        <Select aria-label="Jump to a section" value={active} onChange={(event) => jump(event.target.value)} className="w-full">
          {entries.map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.title}
            </option>
          ))}
        </Select>
      </div>
    </>
  );
}

function contentsOf(report: ResearchReport): Entry[] {
  return [
    ...PROSE.filter(({ key }) => cited(report[key]).length > 0).map(({ id, title }) => ({ id, title })),
    ...(cited(report.news_summary).length > 0 ? [{ id: "news", title: "Recent news" }] : []),
    ...(report.bull_factors.length + report.bear_factors.length > 0
      ? [{ id: "cases", title: "Bull and bear cases" }]
      : []),
    ...(cited(report.risks).length > 0 ? [{ id: "risks", title: "Risks" }] : []),
    { id: "management", title: "Management" },
    ...(report.insufficient_evidence_sections.length + report.failed_sections.length > 0
      ? [{ id: "not-covered", title: "Not covered" }]
      : []),
    { id: "references", title: "References" },
  ];
}

/** The research report as one document (§10.9-§10.16): a reading column with its contents
 *  alongside, every claim carrying its citations. It is AI-written, and says so once, at the
 *  top (FR-010), rather than on every paragraph. */
export function ReportDocument({ envelope }: { envelope: ResearchReportEnvelope }) {
  const { report, ticker } = envelope;
  const { numberOf, order } = buildNumbering(report);
  const risks = cited(report.risks);
  const statements = cited(report.management_commentary);
  const news = cited(report.news_summary);
  const bull = report.bull_factors.filter((factor) => cited(factor.claims).length > 0);
  const bear = report.bear_factors.filter((factor) => cited(factor.claims).length > 0);
  const named = (keys: readonly string[]) => keys.map((key) => SECTION_NAMES[key] ?? words(key));
  const notCovered = named(report.insufficient_evidence_sections);
  const failed = named(report.failed_sections);
  const carried = report.carried_over;

  return (
    <div className="lg:grid lg:grid-cols-[180px_minmax(0,720px)] lg:gap-x-16">
      <Contents entries={contentsOf(report)} />

      <article className="min-w-0 max-w-[720px]">
        <header className="mb-10 border-b border-border pb-6">
          <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
            <div className="flex items-baseline gap-3">
              <h2 className="text-heading font-semibold">AI research</h2>
              <AiMark />
            </div>
            <div className="flex flex-wrap items-center justify-end gap-1 print:hidden">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => downloadText(`${ticker}_research.md`, reportToMarkdown(envelope), "text/markdown")}
              >
                <Download aria-hidden className="size-3.5" />
                Export Markdown
              </Button>
              <Button variant="ghost" size="sm" onClick={() => window.print()}>
                <Printer aria-hidden className="size-3.5" />
                Print
              </Button>
              <RegenerateReport ticker={ticker} />
            </div>
          </div>
          <p className="mt-2 max-w-xl text-sm text-muted">
            Written by an AI model from {ticker}’s filings, reported figures and news. Every claim
            links to its source; where the evidence was not there, the section was left out.
          </p>
          {carried && (
            <p className="mt-2 max-w-xl text-sm text-muted">
              The AI model could not be reached for {named(carried.sections).join(", ")}, so{" "}
              {carried.sections.length === 1 ? "that section is" : "those sections are"} from the
              report of {formatDate(carried.generated_at)}.
            </p>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-x-1 text-caption text-subtle">
            <span>
              {[envelope.generated_at && `Generated ${formatDate(envelope.generated_at)}`, envelope.model]
                .filter(Boolean)
                .join(" · ")}
            </span>
            {(envelope.prompt_version || envelope.data_version) && (
              <>
                {/* Provenance (spec §10.16) matters to whoever checks the report, not to every
                    reader: behind a popover on screen, written out in print. */}
                <span className="print:hidden">
                  <Popover
                    trigger={
                      <Button variant="ghost" size="sm" className="h-auto px-1.5 py-0.5 text-caption text-subtle">
                        Details
                      </Button>
                    }
                  >
                    <dl className="space-y-3">
                      {envelope.prompt_version && (
                        <div>
                          <dt className="text-muted">Prompt version</dt>
                          <dd className="font-mono">{envelope.prompt_version}</dd>
                        </div>
                      )}
                      {envelope.data_version && (
                        <div>
                          <dt className="text-muted">Data fingerprint</dt>
                          <dd className="font-mono">{envelope.data_version}</dd>
                        </div>
                      )}
                    </dl>
                    <p className="mt-3 text-caption text-subtle">
                      The instructions the model was given and the exact set of sources it used. Two
                      reports with the same values were written from the same evidence.
                    </p>
                  </Popover>
                </span>
                <span className="hidden print:inline">
                  {[
                    envelope.prompt_version && `· Prompt ${envelope.prompt_version}`,
                    envelope.data_version && `· Data ${envelope.data_version}`,
                  ]
                    .filter(Boolean)
                    .join(" ")}
                </span>
              </>
            )}
          </div>
        </header>

        <div className="space-y-14">
          {PROSE.map(({ id, key, title }) => {
            const claims = cited(report[key]);
            if (claims.length === 0) return null;
            return (
              <DocSection key={id} id={id} title={title}>
                {claims.map((claim) => (
                  <Claim key={claim.claim_id} claim={claim} numberOf={numberOf} />
                ))}
              </DocSection>
            );
          })}

          {news.length > 0 && (
            <DocSection id="news" title="Recent news">
              <News items={news} numberOf={numberOf} />
            </DocSection>
          )}

          {bull.length + bear.length > 0 && (
            <DocSection id="cases" title="Bull and bear cases">
              {/* Both cases draw on the same evidence pool, side by side so neither is buried. */}
              <div className="grid gap-x-10 gap-y-8 sm:grid-cols-2">
                <Case title="The bull case" tone="positive" factors={bull} numberOf={numberOf} />
                <Case title="The bear case" tone="negative" factors={bear} numberOf={numberOf} />
              </div>
            </DocSection>
          )}

          {risks.length > 0 && (
            <DocSection id="risks" title="Risks">
              <Risks risks={risks} numberOf={numberOf} />
            </DocSection>
          )}

          <DocSection id="management" title="Management">
            {statements.length > 0 ? (
              <Statements statements={statements} numberOf={numberOf} />
            ) : (
              <p className="text-sm text-muted">
                The report found no management statements it could cite. The counts below come
                straight from the filings.
              </p>
            )}
            <div className="pt-4">
              <h4 className="mb-3 text-body font-medium">Language over time</h4>
              <LanguageTracker ticker={ticker} />
            </div>
          </DocSection>

          {notCovered.length + failed.length > 0 && (
            <DocSection id="not-covered" title="Not covered">
              {/* Abstaining is the system working as designed, so it is stated, not styled as a fault. */}
              {notCovered.length > 0 && (
                <p className="text-body leading-relaxed text-muted">
                  Not enough evidence to write about: {notCovered.join(", ")}. Nothing was filled in
                  to cover the gap.
                </p>
              )}
              {/* An outage is not abstention (ADR-0057): say so, and that trying again may work. */}
              {failed.length > 0 && (
                <p className="text-body leading-relaxed text-muted">
                  The AI model could not be reached for {failed.join(", ")}. Regenerate the report
                  to try again.
                </p>
              )}
            </DocSection>
          )}

          <DocSection id="references" title="References">
            <ReferenceList entries={order.map((sourceId, i) => ({ number: i + 1, sourceId }))} />
          </DocSection>
        </div>
      </article>
    </div>
  );
}
