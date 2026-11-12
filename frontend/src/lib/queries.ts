// React Query hooks over the typed API client. Each query validates its payload with Zod
// (schemas.ts) before it reaches a component, so a malformed response fails fast and visibly.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { UseMutationResult, UseQueryResult } from "@tanstack/react-query";

import { apiRequest } from "./apiClient";
import { parse } from "./schemas";
import type {
  Alert,
  AlertCreate,
  AlertEventType,
  AlertUpdate,
  AppNotification,
  CompanyResponse,
  CompanySearchResult,
  ComparisonResponse,
  DataFreshnessResponse,
  DemoInfo,
  EarningsBriefsResponse,
  EarningsCalendarResponse,
  EarningsResponse,
  GenerateBriefResponse,
  FilingDiffResponse,
  FilingSectionsResponse,
  FilingSummary,
  FinancialsResponse,
  JobState,
  ManagementLanguageResponse,
  MetricResult,
  MetricsCoverageResponse,
  EarningsQualityResponse,
  FootnoteFactsResponse,
  GovernanceHistoryResponse,
  GovernanceResponse,
  GuidanceResponse,
  GuidanceTrackRecord,
  PeerSetOut,
  PeerSetsResponse,
  ValuationBandsResponse,
  NotesResponse,
  RedFlagsResponse,
  NewsResponse,
  PeerSuggestionsResponse,
  PortfolioAnalysis,
  PricesResponse,
  ReportDiff,
  ResearchAccepted,
  ResearchReportEnvelope,
  SourceDetail,
  ValuationResponse,
  VerifiedOutput,
  Watchlist,
} from "../types";

const MINUTE = 60_000;

export function useCompanySearch(query: string): UseQueryResult<CompanySearchResult[]> {
  const q = query.trim();
  return useQuery({
    queryKey: ["search", q],
    enabled: q.length >= 1,
    staleTime: 5 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>("/companies/search", { query: { q }, signal });
      return parse.searchResults(data);
    },
  });
}

export function useCompany(ticker: string): UseQueryResult<CompanyResponse> {
  return useQuery({
    queryKey: ["company", ticker],
    staleTime: 5 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}`, { signal });
      return parse.companyResponse(data);
    },
  });
}

export function useFinancials(
  ticker: string,
  metrics: string[],
  periodType: string,
  enabled = true,
): UseQueryResult<FinancialsResponse> {
  const metricsParam = metrics.join(",");
  return useQuery({
    queryKey: ["financials", ticker, metricsParam, periodType],
    enabled,
    staleTime: 5 * MINUTE,
    // Switching Annual ↔ Quarterly keeps the previous figures on screen (dimmed by the caller via
    // `isPlaceholderData`) instead of flashing skeletons. Never across companies: another
    // ticker's numbers must not appear, even for a moment.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === ticker && previousQuery.queryKey[2] === metricsParam
        ? previous
        : undefined,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/financials`, {
        query: { metrics: metricsParam, period_type: periodType },
        signal,
      });
      return parse.financials(data);
    },
  });
}

/** Which quarters and TTM windows each metric has, and why the others are missing (FR-QT-010). */
export function useMetricsCoverage(
  ticker: string,
  enabled = true,
): UseQueryResult<MetricsCoverageResponse> {
  return useQuery({
    queryKey: ["metrics-coverage", ticker],
    enabled,
    staleTime: 5 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/metrics/coverage`, { signal });
      return parse.metricsCoverage(data);
    },
  });
}

/** Reported filing events, newest first, and when the company was last scanned (FR-RF-009). */
export function useRedFlags(ticker: string): UseQueryResult<RedFlagsResponse> {
  return useQuery({
    queryKey: ["red-flags", ticker],
    staleTime: 5 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/red-flags`, {
        query: { limit: "50" },
        signal,
      });
      return parse.redFlags(data);
    },
  });
}

/** Company guidance with outcomes and revisions, newest period first (FR-GD-014). */
export function useGuidance(ticker: string): UseQueryResult<GuidanceResponse> {
  return useQuery({
    queryKey: ["guidance", ticker],
    staleTime: 5 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/guidance`, { signal });
      return parse.guidance(data);
    },
  });
}

/** Revenue against the initial guided range over the last 8 guided quarters (FR-GD-011). */
export function useGuidanceTrackRecord(ticker: string): UseQueryResult<GuidanceTrackRecord> {
  return useQuery({
    queryKey: ["guidance-track-record", ticker],
    staleTime: 5 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/guidance/track-record`, { signal });
      return parse.guidanceTrackRecord(data);
    },
  });
}

/** Reported and upcoming earnings dates, newest quarter first (FR-EB-001/002). */
export function useEarningsCalendar(ticker: string): UseQueryResult<EarningsCalendarResponse> {
  return useQuery({
    queryKey: ["earnings-calendar", ticker],
    staleTime: 5 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/earnings/calendar`, { signal });
      return parse.earningsCalendar(data);
    },
  });
}

/** Stored previews and briefs, every version, latest quarter first (FR-EB-013/015). */
export function useEarningsBriefs(ticker: string): UseQueryResult<EarningsBriefsResponse> {
  return useQuery({
    queryKey: ["earnings-briefs", ticker],
    staleTime: 5 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/earnings/briefs`, { signal });
      return parse.earningsBriefs(data);
    },
  });
}

/** Make (or get back, if nothing changed) the current preview or brief; signed in, rate-limited. */
export function useGenerateBrief(
  ticker: string,
): UseMutationResult<GenerateBriefResponse, Error, "preview" | "brief"> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (kind: "preview" | "brief") => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/earnings/briefs`, {
        method: "POST",
        body: { kind },
      });
      return parse.generatedBrief(data);
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: ["earnings-briefs", ticker] }),
  });
}

/** Earnings-quality measures for the last five fiscal years (FR-EQ-009). */
export function useEarningsQuality(ticker: string): UseQueryResult<EarningsQualityResponse> {
  return useQuery({
    queryKey: ["earnings-quality", ticker],
    staleTime: 5 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/earnings-quality`, { signal });
      return parse.earningsQuality(data);
    },
  });
}

/** The latest 10-K's notes, each with its category and cached AI summary (FR-FN-011). */
export function useNotes(ticker: string): UseQueryResult<NotesResponse> {
  return useQuery({
    queryKey: ["notes", ticker],
    staleTime: 10 * MINUTE,
    retry: false, // 404 = no parsed 10-K yet: an empty state, not a transient failure
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/notes`, { signal });
      return parse.notes(data);
    },
  });
}

/** The latest fiscal year's XBRL footnote facts and gaps, by note category (FR-FN-011). */
export function useFootnoteFacts(ticker: string): UseQueryResult<FootnoteFactsResponse> {
  return useQuery({
    queryKey: ["footnote-facts", ticker],
    staleTime: 10 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/footnote-facts`, { signal });
      return parse.footnoteFacts(data);
    },
  });
}

/** One proxy's governance view (FR-GV-011); `fiscalYear` null = the latest proxy. */
export function useGovernance(ticker: string, fiscalYear: number | null): UseQueryResult<GovernanceResponse> {
  return useQuery({
    queryKey: ["governance", ticker, fiscalYear],
    staleTime: 10 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/governance`, {
        query: { fiscal_year: fiscalYear === null ? undefined : String(fiscalYear) },
        signal,
      });
      return parse.governance(data);
    },
  });
}

/** CEO total pay and board independence by fiscal year, for the trend (FR-GV-011). */
export function useGovernanceHistory(ticker: string): UseQueryResult<GovernanceHistoryResponse> {
  return useQuery({
    queryKey: ["governance-history", ticker],
    staleTime: 10 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/governance/history`, { signal });
      return parse.governanceHistory(data);
    },
  });
}

/** The company's system default peer set and, when signed in, the caller's own (FR-PS-012). */
export function usePeerSets(ticker: string, signedIn: boolean): UseQueryResult<PeerSetsResponse> {
  return useQuery({
    queryKey: ["peer-sets", ticker, signedIn],
    enabled: ticker !== "",
    staleTime: 5 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/peer-sets`, { signal });
      return parse.peerSets(data);
    },
  });
}

/** Each multiple's own-history and peer band (FR-PS-009); `peerSetId` null = the default set. */
export function useValuationBands(ticker: string, peerSetId: number | null): UseQueryResult<ValuationBandsResponse> {
  return useQuery({
    queryKey: ["valuation-bands", ticker, peerSetId],
    staleTime: 5 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/valuation-bands`, {
        query: { peer_set_id: peerSetId === null ? undefined : String(peerSetId) },
        signal,
      });
      return parse.valuationBands(data);
    },
  });
}

export interface PeerSetInput {
  id?: number; // present = update that set
  name: string;
  tickers: string[];
}

/** Create or update the caller's own peer set; the API names unknown tickers (FR-PS-003). */
export function useSavePeerSet(ticker: string): UseMutationResult<PeerSetOut, Error, PeerSetInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, name, tickers }: PeerSetInput) => {
      const path = id === undefined ? `/companies/${ticker}/peer-sets` : `/companies/${ticker}/peer-sets/${id}`;
      const { data } = await apiRequest<unknown>(path, {
        method: id === undefined ? "POST" : "PATCH",
        body: { name, tickers },
      });
      return parse.peerSet(data);
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: ["peer-sets", ticker] }),
  });
}

export function useDeletePeerSet(ticker: string): UseMutationResult<void, Error, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) => {
      await apiRequest<unknown>(`/companies/${ticker}/peer-sets/${id}`, { method: "DELETE" });
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: ["peer-sets", ticker] }),
  });
}

export function useValuation(ticker: string): UseQueryResult<ValuationResponse> {
  return useQuery({
    queryKey: ["valuation", ticker],
    staleTime: 5 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/valuation`, { signal });
      return parse.valuation(data);
    },
  });
}

export function usePrices(ticker: string): UseQueryResult<PricesResponse> {
  return useQuery({
    queryKey: ["prices", ticker],
    staleTime: 5 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/prices`, { signal });
      return parse.prices(data);
    },
  });
}

const RECENT_PRICE_DAYS = 45;

/** The last few weeks of daily bars: enough for a last close and day change in a list row without
 *  downloading the full multi-year series that `usePrices` returns for charts. */
export function useRecentPrices(ticker: string): UseQueryResult<PricesResponse> {
  return useQuery({
    queryKey: ["prices", ticker, "recent"],
    staleTime: 5 * MINUTE,
    retry: false, // a ticker with no prices (or not ingested) just shows no quote
    queryFn: async ({ signal }) => {
      const start = new Date(Date.now() - RECENT_PRICE_DAYS * 86_400_000).toISOString().slice(0, 10);
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/prices`, {
        query: { start },
        signal,
      });
      return parse.prices(data);
    },
  });
}

/** A company's filings. The API pages by id, not by date, so a caller that needs "the latest"
 *  must ask for enough rows to contain them and sort by `filing_date` itself. */
export function useFilings(ticker: string, limit = 25): UseQueryResult<FilingSummary[]> {
  return useQuery({
    queryKey: ["filings", ticker, limit],
    staleTime: 5 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/filings`, {
        query: { limit },
        signal,
      });
      return parse.filings(data);
    },
  });
}

export function useNews(ticker: string): UseQueryResult<NewsResponse> {
  return useQuery({
    queryKey: ["news", ticker],
    staleTime: 5 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/news`, {
        query: { limit: 25 },
        signal,
      });
      return parse.news(data);
    },
  });
}

export function useDataFreshness(ticker: string): UseQueryResult<DataFreshnessResponse> {
  return useQuery({
    queryKey: ["freshness", ticker],
    staleTime: MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/meta/data-freshness/${ticker}`, { signal });
      return parse.dataFreshness(data);
    },
  });
}

export function useSource(sourceId: string | null): UseQueryResult<SourceDetail> {
  return useQuery({
    queryKey: ["source", sourceId],
    enabled: sourceId !== null,
    staleTime: 10 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/sources/${encodeURIComponent(sourceId ?? "")}`, {
        signal,
      });
      return parse.sourceDetail(data);
    },
  });
}

export function useLatestResearch(ticker: string): UseQueryResult<ResearchReportEnvelope> {
  return useQuery({
    queryKey: ["research-latest", ticker],
    retry: false, // a 404 ("no report yet") is an expected state, not a transient error
    staleTime: MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/research/latest`, { signal });
      return parse.researchEnvelope(data);
    },
  });
}

/** What changed since the company's previous report (ADR-0059); a 404 means there is none. */
export function useReportDiff(researchId: string): UseQueryResult<ReportDiff> {
  return useQuery({
    queryKey: ["research-diff", researchId],
    retry: false, // 404 = first report for this company, an expected state
    staleTime: 10 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/research/${researchId}/diff`, { signal });
      return parse.reportDiff(data);
    },
  });
}

export function useFilingSections(filingId: string | null): UseQueryResult<FilingSectionsResponse> {
  return useQuery({
    queryKey: ["filing-sections", filingId],
    enabled: filingId !== null,
    staleTime: 10 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/filings/${filingId}/sections`, { signal });
      return parse.filingSections(data);
    },
  });
}

/** What changed in a filing vs. an older one; `against` null = the prior filing of that type.
 *  `noteCategory` narrows both filings to that note (FR-FN-010). */
export function useFilingDiff(
  filingId: string,
  against: string | null,
  kind: "risk_factor" | null,
  noteCategory: string | null = null,
  enabled = true,
): UseQueryResult<FilingDiffResponse> {
  return useQuery({
    queryKey: ["filing-diff", filingId, against, kind, noteCategory],
    enabled,
    staleTime: 10 * MINUTE,
    retry: false, // 404/422 here mean "nothing to compare", not a transient failure
    queryFn: async ({ signal }) => {
      const path = noteCategory ? `/filings/${filingId}/notes/${noteCategory}/diff` : `/filings/${filingId}/diff`;
      const { data } = await apiRequest<unknown>(path, {
        query: { against: against ?? undefined, kind: kind ?? undefined },
        signal,
      });
      return parse.filingDiff(data);
    },
  });
}

export function useEarningsReactions(ticker: string): UseQueryResult<EarningsResponse> {
  return useQuery({
    queryKey: ["earnings", ticker],
    staleTime: 10 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/earnings`, { signal });
      return parse.earnings(data);
    },
  });
}

export function useManagementLanguage(
  ticker: string,
): UseQueryResult<ManagementLanguageResponse> {
  return useQuery({
    queryKey: ["management-language", ticker],
    staleTime: 10 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/companies/${ticker}/management-language`, {
        signal,
      });
      return parse.managementLanguage(data);
    },
  });
}

export function useStartResearch(): UseMutationResult<ResearchAccepted, Error, string> {
  return useMutation({
    mutationFn: async (ticker: string) => {
      const { data } = await apiRequest<unknown>("/research", {
        method: "POST",
        body: { ticker },
      });
      return parse.researchAccepted(data);
    },
  });
}

export function useJob(jobId: string | null): UseQueryResult<JobState> {
  return useQuery({
    queryKey: ["job", jobId],
    enabled: jobId !== null,
    // Poll while the job is still running; stop once terminal.
    refetchInterval: (query) => {
      const status = (query.state.data as JobState | undefined)?.status;
      return status === "done" || status === "failed" ? false : 2000;
    },
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/research/jobs/${jobId}`, { signal });
      return parse.jobState(data);
    },
  });
}

export function useMetricLineage(
  ticker: string,
  metricName: string,
  period: string,
  enabled: boolean,
): UseQueryResult<MetricResult> {
  return useQuery({
    queryKey: ["lineage", ticker, metricName, period],
    enabled,
    staleTime: 10 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(
        `/companies/${ticker}/metrics/${metricName}/lineage`,
        { query: { period }, signal },
      );
      return parse.metricResult(data);
    },
  });
}

// --- Phase 6a: comparison & portfolio (§37.1/§37.2) ---

export function useComparison(
  tickers: string[],
  metrics: string[],
): UseQueryResult<ComparisonResponse> {
  const tickersParam = tickers.join(",");
  const metricsParam = metrics.join(",");
  return useQuery({
    queryKey: ["compare", tickersParam, metricsParam],
    enabled: tickers.length >= 2,
    staleTime: 5 * MINUTE,
    queryFn: async ({ signal }) => {
      const query: Record<string, string> = { tickers: tickersParam };
      if (metricsParam) query.metrics = metricsParam;
      const { data } = await apiRequest<unknown>("/compare", { query, signal });
      return parse.comparison(data);
    },
  });
}

export function usePeers(ticker: string): UseQueryResult<PeerSuggestionsResponse> {
  return useQuery({
    queryKey: ["peers", ticker],
    enabled: ticker.length > 0,
    staleTime: 10 * MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>(`/compare/peers/${ticker}`, { signal });
      return parse.peers(data);
    },
  });
}

export function useComparisonCommentary(): UseMutationResult<
  VerifiedOutput,
  Error,
  { tickers: string[]; metrics?: string[]; period?: string }
> {
  return useMutation({
    mutationFn: async (body) => {
      const { data } = await apiRequest<unknown>("/compare/commentary", {
        method: "POST",
        body,
      });
      return parse.verifiedOutput(data);
    },
  });
}

export function usePortfolioAnalysis(): UseMutationResult<
  PortfolioAnalysis,
  Error,
  { ticker: string; weight: number }[]
> {
  return useMutation({
    mutationFn: async (holdings) => {
      const { data } = await apiRequest<unknown>("/portfolio/analyze", {
        method: "POST",
        body: { holdings: holdings.map((h) => ({ ticker: h.ticker, weight: String(h.weight) })) },
      });
      return parse.portfolio(data);
    },
  });
}

// --- Watchlists, alerts & notifications (§26.2) ---
// All three belong to the signed-in user: pass their id (it scopes the cache, so one account's
// data is never shown to the next) or null when signed out, which disables the query.

export function useWatchlists(userId: string | null): UseQueryResult<Watchlist[]> {
  return useQuery({
    queryKey: ["watchlists", userId],
    enabled: userId !== null,
    staleTime: MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>("/watchlists", { signal });
      return parse.watchlists(data);
    },
  });
}

export function useCreateWatchlist(): UseMutationResult<Watchlist, Error, string> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (name: string) => {
      const { data } = await apiRequest<unknown>("/watchlists", { method: "POST", body: { name } });
      return parse.watchlist(data);
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["watchlists"] }),
  });
}

export function useDeleteWatchlist(): UseMutationResult<void, Error, string> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      await apiRequest<unknown>(`/watchlists/${id}`, { method: "DELETE" });
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["watchlists"] }),
  });
}

export function useAlerts(userId: string | null): UseQueryResult<Alert[]> {
  return useQuery({
    queryKey: ["alerts", userId],
    enabled: userId !== null,
    staleTime: MINUTE,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>("/alerts", { signal });
      return parse.alerts(data);
    },
  });
}

/** The alert kinds on offer and each one's params as JSON Schema (FR-AL-010). Static per
 *  deploy, so cached for the session. */
export function useAlertEventTypes(): UseQueryResult<AlertEventType[]> {
  return useQuery({
    queryKey: ["alert-event-types"],
    staleTime: Infinity,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>("/alerts/event-types", { signal });
      return parse.alertEventTypes(data);
    },
  });
}

/** Only `event_type` is required; the server fills in the defaults for the rest. */
type NewAlert = Pick<AlertCreate, "event_type"> & Partial<AlertCreate>;

export function useCreateAlert(): UseMutationResult<Alert, Error, NewAlert> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body) => {
      const { data } = await apiRequest<unknown>("/alerts", { method: "POST", body });
      return parse.alert(data);
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["alerts"] }),
  });
}

export function useUpdateAlert(): UseMutationResult<Alert, Error, { id: string } & AlertUpdate> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, ...body }) => {
      const { data } = await apiRequest<unknown>(`/alerts/${id}`, { method: "PATCH", body });
      return parse.alert(data);
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["alerts"] }),
  });
}

export function useDeleteAlert(): UseMutationResult<void, Error, string> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      await apiRequest<unknown>(`/alerts/${id}`, { method: "DELETE" });
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["alerts"] }),
  });
}

/** Newest first. Polled slowly and on returning to the tab: alerts are dispatched by a batch
 *  job, so nothing here is worth a faster cadence. */
export function useNotifications(userId: string | null): UseQueryResult<AppNotification[]> {
  return useQuery({
    queryKey: ["notifications", userId],
    enabled: userId !== null,
    staleTime: 30_000,
    refetchInterval: MINUTE,
    refetchOnWindowFocus: true,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>("/notifications", { query: { limit: 20 }, signal });
      return parse.notifications(data);
    },
  });
}

export function useMarkNotificationsRead(): UseMutationResult<void, Error, string[]> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (ids: string[]) => {
      await Promise.all(
        ids.map((id) => apiRequest<unknown>(`/notifications/${id}/read`, { method: "POST" })),
      );
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });
}

/** Whether this deployment is the public demo, and its fixture date (NFR-ME-007). Fetched once. */
export function useDemoInfo(): UseQueryResult<DemoInfo> {
  return useQuery({
    queryKey: ["demo-info"],
    staleTime: Infinity,
    queryFn: async ({ signal }) => {
      const { data } = await apiRequest<unknown>("/meta/demo", { signal });
      return parse.demoInfo(data);
    },
  });
}
