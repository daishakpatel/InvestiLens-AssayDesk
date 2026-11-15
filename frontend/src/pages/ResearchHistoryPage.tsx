import { Link } from "react-router-dom";

import { SignInHint } from "../components/SignInHint";
import { Container } from "../components/ui/Container";
import { EmptyState, ErrorState, LoadingState } from "../components/ui/States";
import { useAuth } from "../lib/auth";
import { formatDate } from "../lib/format";
import { useMyResearch } from "../lib/queries";
import { useDocumentTitle } from "../lib/useDocumentTitle";

function History({ userId }: { userId: string }) {
  const history = useMyResearch(userId);

  if (history.isError) {
    return <ErrorState message="Your research could not be loaded." onRetry={() => void history.refetch()} />;
  }
  if (!history.data) return <LoadingState label="Loading your research" />;
  if (history.data.length === 0) {
    return <EmptyState message="No reports yet. Open a company’s Research tab to generate one." />;
  }
  return (
    <ul className="max-w-2xl divide-y divide-border border-y border-border">
      {history.data.map((row) => (
        <li key={row.research_id}>
          <Link
            to={`/report/${row.research_id}`}
            className="flex items-baseline gap-4 py-3 transition-colors hover:bg-surface-2"
          >
            <span className="w-16 font-medium">{row.ticker}</span>
            <span className="text-muted">{formatDate(row.generated_at)}</span>
            {row.model && <span className="ml-auto text-sm text-subtle">{row.model}</span>}
          </Link>
        </li>
      ))}
    </ul>
  );
}

/** Research history (§26.2): every report the reader generated, newest first, each at its own
 *  shareable address. */
export default function ResearchHistoryPage() {
  const { user } = useAuth();
  useDocumentTitle("Your research");

  return (
    <Container className="space-y-10 py-10">
      <header>
        <h1 className="text-heading font-semibold">Your research</h1>
        <p className="mt-1 text-muted">Reports you generated, newest first.</p>
      </header>
      {user ? (
        <History userId={user.id} />
      ) : (
        <SignInHint>Sign in to see the research reports you have generated.</SignInHint>
      )}
    </Container>
  );
}
