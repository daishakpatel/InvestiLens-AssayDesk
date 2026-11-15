import { Link } from "react-router-dom";

import { SignInHint } from "../components/SignInHint";
import { Container } from "../components/ui/Container";
import { Section } from "../components/ui/Section";
import { EmptyState, ErrorState, LoadingState } from "../components/ui/States";
import { useAuth } from "../lib/auth";
import { formatDate } from "../lib/format";
import { useDemoInfo, useMyQuestions, useMyResearch } from "../lib/queries";
import { useDocumentTitle } from "../lib/useDocumentTitle";
import type { AskedQuestion } from "../types";

const LIST = "max-w-2xl divide-y divide-border border-b border-border";
const ROW = "flex items-baseline gap-4 py-3 transition-colors hover:bg-surface-2";

function Reports({ userId }: { userId: string }) {
  const history = useMyResearch(userId);

  if (history.isError) {
    return <ErrorState message="Your reports could not be loaded." onRetry={() => void history.refetch()} />;
  }
  if (!history.data) return <LoadingState label="Loading your reports" />;
  if (history.data.length === 0) {
    return <EmptyState message="No reports yet. Open a company’s Research tab to generate one." />;
  }
  return (
    <ul className={LIST}>
      {history.data.map((row) => (
        <li key={row.research_id}>
          <Link to={`/report/${row.research_id}`} className={ROW}>
            <span className="w-16 shrink-0 font-medium">{row.ticker}</span>
            <span className="text-muted">{formatDate(row.generated_at)}</span>
            {row.model && <span className="ml-auto text-sm text-subtle">{row.model}</span>}
          </Link>
        </li>
      ))}
    </ul>
  );
}

/** The same question about the same company once, at its latest asking (the list is newest first). */
function distinct(questions: AskedQuestion[]): AskedQuestion[] {
  const seen = new Set<string>();
  return questions.filter((q) => {
    const key = `${q.ticker}|${q.question.trim().toLowerCase()}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function Questions({ userId }: { userId: string }) {
  const asked = useMyQuestions(userId);
  const demo = useDemoInfo().data?.enabled === true;

  if (demo) {
    return <EmptyState message="Past questions aren’t kept in the demo: every visitor shares one account." />;
  }

  if (asked.isError) {
    return <ErrorState message="Your questions could not be loaded." onRetry={() => void asked.refetch()} />;
  }
  if (!asked.data) return <LoadingState label="Loading your questions" />;
  if (asked.data.length === 0) {
    return <EmptyState message="No questions yet. Ask the assistant on any company page." />;
  }
  return (
    <ul className={LIST}>
      {distinct(asked.data).map((q) => (
        <li key={q.message_id}>
          {/* Asks it again: filings and prices move, so a fresh answer beats a stored one. */}
          <Link to={`/company/${q.ticker}/chat?ask=${encodeURIComponent(q.question)}`} className={ROW}>
            <span className="w-16 shrink-0 font-medium">{q.ticker}</span>
            <span className="min-w-0 flex-1 truncate">{q.question}</span>
            <span className="shrink-0 text-sm text-subtle">{formatDate(q.asked_at)}</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

/** Research history and saved questions (§26.2): every report the reader generated, each at its
 *  own shareable address, and every question they asked, one click from asking it again. */
export default function ResearchHistoryPage() {
  const { user } = useAuth();
  useDocumentTitle("Your research");

  return (
    <Container className="space-y-10 py-10">
      <header>
        <h1 className="text-heading font-semibold">Your research</h1>
        <p className="mt-1 text-muted">Reports you generated and questions you asked, newest first.</p>
      </header>
      {user ? (
        <>
          <Section title="Reports">
            <div>
              <Reports userId={user.id} />
            </div>
          </Section>
          <Section title="Questions">
            <div>
              <Questions userId={user.id} />
            </div>
          </Section>
        </>
      ) : (
        <SignInHint>Sign in to see the reports you generated and the questions you asked.</SignInHint>
      )}
    </Container>
  );
}
