import { useParams } from "react-router-dom";

import { LanguageTracker } from "../../components/filings/LanguageTracker";
import { GenerateReport } from "../../components/research/GenerateReport";
import { ReportDocument } from "../../components/research/ReportDocument";
import { Section } from "../../components/ui/Section";
import { Skeleton } from "../../components/ui/Skeleton";
import { ApiError } from "../../lib/apiClient";
import { useApiStatus } from "../../lib/apiStatus";
import { useLatestResearch } from "../../lib/queries";
import { useScrollToHash } from "../../lib/useScrollToHash";

export function DocumentSkeleton() {
  return (
    <div role="status" aria-label="Loading research" className="max-w-[720px] space-y-4">
      <Skeleton className="h-7 w-48" />
      <Skeleton className="w-3/4" />
      <Skeleton shape="block" className="mt-8 h-32" />
      <Skeleton shape="block" className="h-32" />
    </div>
  );
}

/** Research: the AI report as one document when there is one, or what a report is and the way
 *  to make it when there is not. Management language tracking is deterministic and shows either
 *  way. The old Risks and Management tabs redirect to `#risks` and `#management` here. */
export default function ResearchTab() {
  const { ticker = "" } = useParams();
  const latest = useLatestResearch(ticker);
  const { online, reachable } = useApiStatus();
  useScrollToHash(!latest.isPending); // the sections exist only once the report has loaded

  if (latest.isPending || (latest.isError && !(online && reachable))) return <DocumentSkeleton />;
  if (latest.isSuccess) return <ReportDocument envelope={latest.data} />;

  // A 404 means no report has been generated. Anything else means one exists but could not be
  // read, which is said plainly rather than passed off as "no report yet".
  const unreadable = !(latest.error instanceof ApiError && latest.error.status === 404);
  return (
    <div className="space-y-16">
      <GenerateReport ticker={ticker} unreadable={unreadable} />
      <div id="management" className="md:scroll-mt-4">
        <Section title="Management language over time">
          <div className="pt-5">
            <LanguageTracker ticker={ticker} />
          </div>
        </Section>
      </div>
    </div>
  );
}
