import { lazy, useEffect, useRef } from "react";
import { Navigate, Outlet, Route, Routes, useLocation, useNavigationType } from "react-router-dom";

import { ApiStatusBanner } from "./components/ApiStatusBanner";
import { AppShell } from "./components/AppShell";
import { CommandPaletteProvider } from "./components/CommandPaletteContext";
import { RouteProgress } from "./components/RouteProgress";
import { Container } from "./components/ui/Container";
import { NotFound } from "./pages/NotFoundPage";

// Route-level code splitting (UI-004). Recharts (Financials) and the report/chat views each get
// their own chunk, out of the initial bundle.
const HomePage = lazy(() => import("./pages/HomePage"));
const AuthPage = lazy(() => import("./pages/AuthPage"));
const CompanyDashboard = lazy(() => import("./pages/CompanyDashboard"));
// Overview draws charts, so it is lazy too: Recharts must stay out of the entry bundle.
const OverviewTab = lazy(() => import("./pages/tabs/OverviewTab"));
const FinancialsTab = lazy(() => import("./pages/tabs/FinancialsTab"));
const EarningsTab = lazy(() => import("./pages/tabs/EarningsTab"));
const FootnotesTab = lazy(() => import("./pages/tabs/FootnotesTab"));
const GovernanceTab = lazy(() => import("./pages/tabs/GovernanceTab"));
const ResearchTab = lazy(() => import("./pages/tabs/ResearchTab"));
const FilingsTab = lazy(() => import("./pages/tabs/FilingsTab"));
const NewsTab = lazy(() => import("./pages/tabs/NewsTab"));
const OpenAssistant = lazy(() => import("./components/panel/OpenAssistant"));
const ComparePage = lazy(() => import("./pages/ComparePage"));
const PortfolioPage = lazy(() => import("./pages/PortfolioPage"));
const AlertsPage = lazy(() => import("./pages/AlertsPage"));
const ThesesPage = lazy(() => import("./pages/ThesesPage"));
const ThesisTab = lazy(() => import("./pages/tabs/ThesisTab"));
const DemoPage = lazy(() => import("./pages/DemoPage"));
const ReportPage = lazy(() => import("./pages/ReportPage"));
const ResearchHistoryPage = lazy(() => import("./pages/ResearchHistoryPage"));
// Design-system review surface: dev server only, so it never ships in the production bundle.
const UiGallery = import.meta.env.DEV ? lazy(() => import("./pages/UiGallery")) : null;

/** The first two path segments: "/company/NVDA/filings" and "/company/NVDA" are one place. */
function placeOf(pathname: string): string {
  return pathname.split("/").slice(0, 3).join("/");
}

/** Arriving somewhere new starts at the top. Moving between a company's tabs does not (that page
 *  manages its own scroll), and Back/Forward keep the position the browser restores. */
function useScrollReset(): void {
  const { pathname } = useLocation();
  const navigationType = useNavigationType();
  const previous = useRef(pathname);
  useEffect(() => {
    const changedPlace = placeOf(previous.current) !== placeOf(pathname);
    previous.current = pathname;
    if (changedPlace && navigationType !== "POP") window.scrollTo(0, 0);
  }, [pathname, navigationType]);
}

/** Wraps every route: the command palette (so its shortcuts work everywhere, including the
 *  sign-in page), the loading line, and the single outage banner. */
function RootLayout() {
  useScrollReset();
  return (
    <CommandPaletteProvider>
      <RouteProgress />
      <Outlet />
      <ApiStatusBanner />
    </CommandPaletteProvider>
  );
}

export function App() {
  return (
    <Routes>
      <Route element={<RootLayout />}>
        {/* Sign-in sits on the bare canvas, outside the app shell. */}
        <Route path="login" element={<AuthPage />} />

        <Route element={<AppShell />}>
          <Route index element={<HomePage />} />
          <Route path="compare" element={<ComparePage />} />
          <Route path="portfolio" element={<PortfolioPage />} />
          <Route path="alerts" element={<AlertsPage />} />
          <Route path="theses" element={<ThesesPage />} />
          <Route path="demo" element={<DemoPage />} />
          <Route path="report/:researchId" element={<ReportPage />} />
          <Route path="research" element={<ResearchHistoryPage />} />
          <Route path="company/:ticker" element={<CompanyDashboard />}>
            <Route index element={<OverviewTab />} />
            <Route path="financials" element={<FinancialsTab />} />
            <Route path="earnings" element={<EarningsTab />} />
            <Route path="thesis" element={<ThesisTab />} />
            <Route path="footnotes" element={<FootnotesTab />} />
            <Route path="governance" element={<GovernanceTab />} />
            <Route path="research" element={<ResearchTab />} />
            <Route path="filings" element={<FilingsTab />} />
            <Route path="news" element={<NewsTab />} />
            {/* Not a tab: a link that opens the assistant panel over the company page. */}
            <Route path="chat" element={<OpenAssistant />} />

            {/* Pre-rebuild tab URLs keep working: each redirects to where its content now lives. */}
            <Route path="overview" element={<Navigate to=".." replace />} />
            <Route
              path="quality"
              element={<Navigate to={{ pathname: "../earnings", hash: "#quality" }} replace />}
            />
            <Route
              path="valuation"
              element={<Navigate to={{ pathname: "../financials", hash: "#valuation" }} replace />}
            />
            <Route
              path="risks"
              element={<Navigate to={{ pathname: "../research", hash: "#risks" }} replace />}
            />
            <Route
              path="management"
              element={<Navigate to={{ pathname: "../research", hash: "#management" }} replace />}
            />
            <Route path="ownership" element={<Navigate to=".." replace />} />
            <Route path="*" element={<Navigate to=".." replace />} />
          </Route>
          {UiGallery && (
            <Route
              path="_ui"
              element={
                <Container>
                  <UiGallery />
                </Container>
              }
            />
          )}
          <Route path="*" element={<NotFound />} />
        </Route>
      </Route>
    </Routes>
  );
}
