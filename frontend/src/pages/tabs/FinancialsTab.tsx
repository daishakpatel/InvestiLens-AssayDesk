import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { MetricChart } from "../../charts/MetricChart";
import { formatValue } from "../../charts/series";
import { StatementTable } from "../../components/company/StatementTable";
import { FreshnessBadge } from "../../components/ui/Badge";
import { Section } from "../../components/ui/Section";
import { ValuationBands } from "../../components/valuation/ValuationBands";
import { SegmentedControl } from "../../components/ui/SegmentedControl";
import { Stat } from "../../components/ui/Stat";
import { ErrorState } from "../../components/ui/States";
import { Switch } from "../../components/ui/Switch";
import { useApiStatus } from "../../lib/apiStatus";
import { availabilityNote, latestTtmAvailability } from "../../lib/coverage";
import { toNumber } from "../../lib/format";
import { useActiveSection } from "../../lib/useActiveSection";
import {
  ALL_FINANCIAL_METRICS,
  FINANCIAL_SECTIONS,
  metricDisplay,
  metricLabel,
  reasonText,
} from "../../lib/metricCatalog";
import type { FinancialSection } from "../../lib/metricCatalog";
import { useFinancials, useMetricLineage, useMetricsCoverage, useValuation } from "../../lib/queries";
import { useScrollToHash } from "../../lib/useScrollToHash";
import type { MetricSeries, MetricsCoverageResponse } from "../../types";

type Period = "FY" | "Q" | "TTM";

const PERIODS: { value: Period; label: string; noun: string; missing: string }[] = [
  { value: "FY", label: "Annual", noun: "Fiscal years", missing: "" },
  {
    value: "Q",
    label: "Quarterly",
    noun: "Fiscal quarters",
    missing: "Quarterly figures are not available for this company yet.",
  },
  {
    value: "TTM",
    label: "TTM",
    noun: "Trailing twelve months",
    missing: "Trailing-twelve-month figures need four reported quarters in a row; none are available yet.",
  },
];

const NAV = [...FINANCIAL_SECTIONS.map(({ id, title }) => ({ id, title })), { id: "valuation", title: "Valuation" }];
// Sections clear the pinned controls row (md and up, where it is sticky) when jumped to.
const ANCHOR = "md:scroll-mt-14";

const hasPoints = (series: MetricSeries | undefined) => (series?.points.length ?? 0) > 0;

function SectionNav() {
  const active = useActiveSection(NAV.map((item) => item.id));
  const navigate = useNavigate();
  return (
    <nav aria-label="Financial sections" className="min-w-0">
      <ul className="flex gap-1 overflow-x-auto [scrollbar-width:none]">
        {NAV.map((item) => (
          <li key={item.id}>
            <a
              href={`#${item.id}`}
              aria-current={active === item.id ? "location" : undefined}
              onClick={(event) => {
                event.preventDefault();
                document.getElementById(item.id)?.scrollIntoView();
                void navigate({ hash: item.id }, { replace: true });
              }}
              className={`flex h-7 items-center whitespace-nowrap rounded-full px-3 text-label font-medium transition-colors pointer-coarse:h-10 ${
                active === item.id ? "bg-surface-2 text-text" : "text-muted hover:text-text"
              }`}
            >
              {item.title}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

/** One chart, or the reason there is none. A metric that is N/A in every period asks the API
 *  why (DR-041) and says so, instead of drawing an empty plot. */
function MetricPanel({
  ticker,
  name,
  series,
  period,
  showChange,
  loading,
  note,
}: {
  ticker: string;
  name: string;
  series: MetricSeries | undefined;
  period: Period;
  showChange: boolean;
  loading: boolean;
  note: string | null; // TTM: "3 of 4 quarters available" (FR-QT-010)
}) {
  const points = series?.points ?? [];
  const allNull = points.length > 0 && points.every((point) => toNumber(point.value) === null);
  // Sector N/A (a bank's operating income) is known without asking the API why (DR-042).
  const notApplicable = series?.applicable === false && (points.length === 0 || allNull);
  const reason = useMetricLineage(ticker, name, points.at(-1)?.period ?? "", allNull && !notApplicable);
  const emptyMessage = notApplicable
    ? `${reasonText("NOT_APPLICABLE_SECTOR")}.`
    : allNull
      ? `${reasonText(reason.data?.warnings[0])}.`
      : (note ?? `${metricLabel(name)} has not been reported in the filings we hold.`);
  return (
    <MetricChart
      name={name}
      points={points}
      csvName={`${ticker}_${name}_${period}`}
      showChange={showChange}
      loading={loading}
      emptyMessage={emptyMessage}
      note={points.length > 0 ? note : null}
    />
  );
}

function StatementSection({
  ticker,
  section,
  series,
  period,
  showChange,
  loading,
  coverage,
}: {
  ticker: string;
  section: FinancialSection;
  series: MetricSeries[];
  period: Period;
  showChange: boolean;
  loading: boolean;
  coverage: MetricsCoverageResponse | undefined;
}) {
  const find = (name: string) => series.find((s) => s.metric_name === name);
  // A TTM card whose latest window is short of four quarters says how many exist (FR-QT-010).
  const noteFor = (name: string) =>
    period === "TTM" ? availabilityNote(latestTtmAvailability(coverage, name)) : null;
  // Annual shows every chart, explaining any that is N/A. Other period types hold far fewer
  // metrics, so only the ones with data, or with a quarter count to explain, are drawn rather
  // than a grid of apologies.
  const charts =
    period === "FY" || loading
      ? section.charts
      : section.charts.filter((name) => hasPoints(find(name)) || noteFor(name) !== null);
  const noun = PERIODS.find((p) => p.value === period)?.noun ?? "";
  const empty = !loading && charts.length === 0 && !section.table.some((name) => hasPoints(find(name)));

  return (
    <div id={section.id} className={ANCHOR}>
      <Section title={section.title}>
        {empty ? (
          <p className="pt-4 text-sm text-muted">
            No {noun.toLowerCase()} figures for this section yet. Switch to Annual to see them.
          </p>
        ) : (
          <div className="space-y-10 pt-6">
            <div className="grid gap-x-12 gap-y-10 md:grid-cols-2">
              {charts.map((name) => (
                <MetricPanel
                  key={name}
                  ticker={ticker}
                  name={name}
                  series={find(name)}
                  period={period}
                  showChange={showChange}
                  loading={loading}
                  note={noteFor(name)}
                />
              ))}
            </div>
            {!loading && (
              <StatementTable
                ticker={ticker}
                title={section.title}
                names={section.table}
                series={series}
                periodNoun={noun}
              />
            )}
          </div>
        )}
      </Section>
    </div>
  );
}

function Valuation({ ticker }: { ticker: string }) {
  const valuation = useValuation(ticker);
  const metrics = (valuation.data?.metrics ?? []).flatMap((metric) => {
    const value = toNumber(metric.value);
    return value === null ? [] : [{ name: metric.formula_id, value, unit: metric.unit }];
  });
  return (
    <div id="valuation" className={ANCHOR}>
      <Section title="Valuation" freshness={valuation.data?.freshness}>
        {metrics.length > 0 ? (
          <div className="grid grid-cols-2 gap-x-8 gap-y-5 pt-6 sm:flex sm:flex-wrap sm:gap-x-12">
            {metrics.map((metric) => (
              <Stat
                key={metric.name}
                label={metricLabel(metric.name)}
                value={formatValue(metric.value, metricDisplay(metric.name, metric.unit))}
                meta={{ source: "Market data and SEC filings" }}
              />
            ))}
          </div>
        ) : (
          <p className="max-w-2xl pt-4 text-sm text-muted">
            {valuation.isPending
              ? "Loading valuation…"
              : "Price-based ratios such as market cap, P/E and EV/EBITDA are not computed yet, so there is nothing to show here. Everything above comes straight from the filings."}
          </p>
        )}
        <ValuationBands ticker={ticker} />
      </Section>
    </div>
  );
}

/** Financials: one row of controls that scopes everything below it, then the statements as
 *  sections of charts with the full table beneath each, and valuation last. */
export default function FinancialsTab() {
  const { ticker = "" } = useParams();
  const [period, setPeriod] = useState<Period>("FY");
  const [showChange, setShowChange] = useState(false);
  const financials = useFinancials(ticker, ALL_FINANCIAL_METRICS, period);
  // Which period types hold anything at all for this company, so the others can say so up front.
  const quarterly = useFinancials(ticker, ["revenue"], "Q");
  const trailing = useFinancials(ticker, ["revenue"], "TTM");
  const coverage = useMetricsCoverage(ticker, period === "TTM");
  const { online, reachable } = useApiStatus();
  useScrollToHash();

  const available: Record<Period, boolean | undefined> = {
    FY: true,
    Q: quarterly.data ? quarterly.data.series.some(hasPoints) : undefined,
    TTM: trailing.data ? trailing.data.series.some(hasPoints) : undefined,
  };
  const loading = financials.isPending;
  const series = financials.data?.series ?? [];

  return (
    <div>
      <div className="z-10 mb-4 flex flex-wrap items-center justify-between gap-x-6 gap-y-3 border-b border-border bg-bg/85 py-2.5 backdrop-blur-md md:sticky md:top-[6.5rem]">
        <SectionNav />
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
          <SegmentedControl
            label="Period"
            value={period}
            onValueChange={setPeriod}
            options={PERIODS.map((p) => ({
              value: p.value,
              label: p.label,
              disabled: available[p.value] !== true,
              ...(available[p.value] === false ? { hint: p.missing } : {}),
            }))}
          />
          <Switch checked={showChange} onCheckedChange={setShowChange}>
            Show growth
          </Switch>
        </div>
      </div>

      {financials.isError && online && reachable ? (
        <ErrorState
          message={financials.error instanceof Error ? financials.error.message : "The financials could not be loaded."}
          onRetry={() => void financials.refetch()}
        />
      ) : (
        // While another period type loads, the previous figures stay in place, dimmed.
        <div
          aria-busy={financials.isPlaceholderData}
          className={`transition-opacity ${financials.isPlaceholderData ? "opacity-50" : ""}`}
        >
          <p className="mb-10 flex flex-wrap items-center gap-x-3 text-caption text-subtle">
            <span>
              {period === "Q"
                ? "The latest eight fiscal quarters. Fourth quarters, and cash-flow quarters after the first, are derived from annual and year-to-date figures."
                : period === "TTM"
                  ? "Trailing twelve months: the sum of four consecutive quarters, shown only when all four are reported."
                  : "Figures as reported in SEC filings; ratios are computed from them."}
            </span>
            {financials.data && <FreshnessBadge freshness={financials.data.freshness} />}
          </p>
          <div className="space-y-16">
            {FINANCIAL_SECTIONS.map((section) => (
              <StatementSection
                key={section.id}
                ticker={ticker}
                section={section}
                series={series}
                period={period}
                showChange={showChange}
                loading={loading}
                coverage={coverage.data}
              />
            ))}
            <Valuation ticker={ticker} />
          </div>
        </div>
      )}
    </div>
  );
}
