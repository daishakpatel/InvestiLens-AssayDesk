import { ArrowLeft } from "lucide-react";
import { Link, useParams } from "react-router-dom";

import { PanelProvider } from "../components/panel/PanelProvider";
import { ReportDocument } from "../components/research/ReportDocument";
import { Container } from "../components/ui/Container";
import { ApiError } from "../lib/apiClient";
import { useResearch } from "../lib/queries";
import { useDocumentTitle } from "../lib/useDocumentTitle";
import { NotFound } from "./NotFoundPage";
import { DocumentSkeleton } from "./tabs/ResearchTab";

/** One stored report at its own address, so it can be shared. Reports are public (built from
 *  public filings), so the link is plain and not revocable (ADR-0061). */
export default function ReportPage() {
  const { researchId = "" } = useParams();
  const report = useResearch(researchId);
  useDocumentTitle(report.data ? `${report.data.ticker} research` : "Research report");

  if (report.isError && report.error instanceof ApiError && report.error.status === 404) {
    return <NotFound title="Report not found" message="No report has this address. It may have been removed." />;
  }
  return (
    <PanelProvider>
      <Container className="py-10">
        {report.data && (
          <Link
            to={`/company/${report.data.ticker}/research`}
            className="mb-8 inline-flex items-center gap-1.5 text-sm text-muted hover:text-text"
          >
            <ArrowLeft aria-hidden className="size-3.5" />
            {report.data.ticker} latest research
          </Link>
        )}
        {report.isSuccess ? (
          <ReportDocument envelope={report.data} />
        ) : report.isError ? (
          <p role="alert" className="text-muted">
            This report couldn’t be loaded. It may be in an older format; open the company’s latest
            research instead.
          </p>
        ) : (
          <DocumentSkeleton />
        )}
      </Container>
    </PanelProvider>
  );
}
