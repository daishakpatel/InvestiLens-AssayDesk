// Single source of UI types: the OpenAPI-generated client (UI-001). No hand-written duplicates —
// these aliases just give the generated `components["schemas"][...]` shapes readable names.
import type { components } from "./services/api";

type Schemas = components["schemas"];

export type CompanyResponse = Schemas["CompanyResponse"];
export type CompanySearchResult = Schemas["CompanySearchResult"];
export type Freshness = Schemas["Freshness"];
export type FinancialsResponse = Schemas["FinancialsResponse"];
export type MetricSeries = Schemas["MetricSeries"];
export type MetricSeriesPoint = Schemas["MetricSeriesPoint"];
export type MetricResult = Schemas["MetricResult"];
export type MetricsCoverageResponse = Schemas["MetricsCoverageResponse"];
export type MetricCoverage = Schemas["MetricCoverage"];
export type EarningsQualityResponse = Schemas["EarningsQualityResponse"];
export type QualityMeasure = Schemas["QualityMeasure"];
export type QualityPoint = Schemas["QualityPoint"];
export type RedFlagsResponse = Schemas["RedFlagsResponse"];
export type RedFlagItem = Schemas["RedFlagItem"];
export type NotesResponse = Schemas["NotesResponse"];
export type NoteItem = Schemas["NoteItem"];
export type NoteBullet = Schemas["NoteBullet"];
export type FootnoteFactsResponse = Schemas["FootnoteFactsResponse"];
export type FootnoteCategory = Schemas["FootnoteCategory"];
export type FootnoteFact = Schemas["FootnoteFact"];
export type GovernanceResponse = Schemas["GovernanceResponse"];
export type GovernanceField = Schemas["GovernanceField"];
export type CompensationRow = Schemas["CompensationRow"];
export type PayVersusPerformancePoint = Schemas["PayVersusPerformancePoint"];
export type GovernanceHistoryResponse = Schemas["GovernanceHistoryResponse"];
export type PeerSetsResponse = Schemas["PeerSetsResponse"];
export type PeerSetOut = Schemas["PeerSetOut"];
export type ValuationBandsResponse = Schemas["ValuationBandsResponse"];
export type ValuationBand = Schemas["ValuationBand"];
export type ValuationResponse = Schemas["ValuationResponse"];
export type GuidanceResponse = Schemas["GuidanceResponse"];
export type GuidanceRow = Schemas["GuidanceRow"];
export type GuidanceValue = Schemas["GuidanceValue"];
export type GuidanceTrackRecord = Schemas["GuidanceTrackRecord"];
export type TrackRecordQuarter = Schemas["TrackRecordQuarter"];
export type EarningsCalendarResponse = Schemas["EarningsCalendarResponse"];
export type CalendarEvent = Schemas["CalendarEvent"];
export type EarningsBriefsResponse = Schemas["EarningsBriefsResponse"];
export type StoredBrief = Schemas["StoredBrief"];
export type EarningsPreview = Schemas["EarningsPreview"];
export type EarningsBrief = Schemas["EarningsBrief"];
export type BriefHeader = Schemas["BriefHeader"];
export type MetricTable = Schemas["MetricTable"];
export type MetricRow = Schemas["MetricRow"];
export type BriefReaction = Schemas["Reaction"];
export type BriefMissing = Schemas["Missing"];
export type WatchItem = Schemas["WatchItem"];
export type GenerateBriefResponse = Schemas["GenerateBriefResponse"];
export type ThesisSummary = Schemas["ThesisSummary"];
export type ThesisDetail = Schemas["ThesisDetail"];
export type ThesisKpi = Schemas["Kpi"];
export type ThesisEvent = Schemas["ThesisEvent"];
export type ThesisRevision = Schemas["ThesisRevision"];
export type MetricRegistryResponse = Schemas["MetricRegistryResponse"];
export type MetricRegistryEntry = Schemas["MetricRegistryEntry"];
export type ThesisCreate = Schemas["ThesisCreate"];
export type ThesisUpdate = Schemas["ThesisUpdate"];
export type KpiCreate = Schemas["KpiCreate"];
export type KpiUpdate = Schemas["KpiUpdate"];
export type DraftResponse = Schemas["DraftResponse"];
export type DraftKpi = Schemas["DraftKpi"];
export type PricesResponse = Schemas["PricesResponse"];
export type FilingSummary = Schemas["FilingSummary"];
export type NewsResponse = Schemas["NewsResponse"];
export type NewsItem = Schemas["NewsItem"];
export type DataFreshnessResponse = Schemas["DataFreshnessResponse"];
export type TokenResponse = Schemas["TokenResponse"];
export type UserProfile = Schemas["UserProfile"];
export type DemoInfo = Schemas["DemoInfo"];

// --- Phase 4d: research report, citations, chat ---
export type SourceDetail = Schemas["SourceDetail"];
export type SourceRecord = SourceDetail["source"];
export type ResearchReportEnvelope = Schemas["ResearchReportEnvelope"];
export type ResearchReport = Schemas["ResearchReport"];
export type ResearchClaim = Schemas["ResearchClaim"];
export type Risk = Schemas["Risk"];
export type Factor = Schemas["Factor"];
export type NewsItemSummary = Schemas["NewsItemSummary"];
export type ManagementTopicStatement = Schemas["ManagementTopicStatement"];
export type ResearchAccepted = Schemas["ResearchAccepted"];
export type ReportDiff = Schemas["ReportDiff"];
export type ReportDiffItem = Schemas["ReportDiffItem"];
export type ReportMetricChange = Schemas["ReportMetricChange"];
export type JobState = Schemas["JobState"];
export type EvidenceLabel = NonNullable<ResearchClaim["confidence_label"]>;
export type ChatResponse = Schemas["ChatResponse"];
// Two `Citation` schemas now coexist in the contract (chat's and the citation pipeline's, exposed
// via /compare/commentary), so FastAPI fully-qualifies both names (Phase 6a).
export type ChatCitation = Schemas["app__schemas__chat__Citation"];
export type ToolTraceEntry = Schemas["ToolTraceEntry"];
export type FilingSectionsResponse = Schemas["FilingSectionsResponse"];

// --- Phase 6b: filing diff, earnings reaction, management language (§37.3-§37.5) ---
export type FilingDiffResponse = Schemas["FilingDiffResponse"];
export type FilingDiffItem = Schemas["FilingDiffItem"];
export type DiffOp = Schemas["DiffOp"];
export type EarningsResponse = Schemas["EarningsResponse"];
export type ManagementLanguageResponse = Schemas["ManagementLanguageResponse"];
export type LanguageSeries = Schemas["LanguageSeries"];
export type LanguagePoint = Schemas["LanguagePoint"];

// --- Watchlists, alerts & notifications (§26.2) ---
export type Watchlist = Schemas["Watchlist"];
export type Alert = Schemas["Alert"];
export type AlertCreate = Schemas["AlertCreate"];
export type AlertUpdate = Schemas["AlertUpdate"];
export type AlertEventType = Schemas["AlertEventType"];
export type AppNotification = Schemas["Notification"]; // `Notification` is a DOM global

// --- Phase 6a: company comparison & portfolio analysis (§37.1/§37.2) ---
export type ComparisonResponse = Schemas["ComparisonResponse"];
export type ComparisonCompany = Schemas["ComparisonCompany"];
export type ComparisonMetricRow = Schemas["ComparisonMetricRow"];
export type ComparisonCell = Schemas["ComparisonCell"];
export type PeerSuggestionsResponse = Schemas["PeerSuggestionsResponse"];
export type VerifiedOutput = Schemas["VerifiedOutput"];
export type PortfolioAnalysis = Schemas["PortfolioAnalysis"];
export type RiskStats = Schemas["RiskStats"];
export type HoldingData = Schemas["HoldingData"];
