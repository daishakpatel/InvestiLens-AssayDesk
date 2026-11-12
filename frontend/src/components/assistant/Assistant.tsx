import { ArrowUp, Square } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { KeyboardEvent, RefObject } from "react";
import { Link } from "react-router-dom";

import { useAuth } from "../../lib/auth";
import { CitedText } from "../citations/CitedText";
import { ReferenceList } from "../citations/ReferenceList";
import { AiMark } from "../ui/AiMark";
import { Feedback } from "../ui/Feedback";
import { Badge } from "../ui/Badge";
import { IconButton } from "../ui/Button";
import { Spinner } from "../ui/Skeleton";
import { MAX_QUESTION_LENGTH } from "./useConversation";
import type { AssistantMessage, Conversation } from "./useConversation";

function suggestedQuestions(ticker: string): string[] {
  return [
    `What was ${ticker}’s revenue in the latest fiscal year?`,
    "How has gross margin changed over the last few years?",
    "What are the biggest risks the company describes?",
    "What does management say about demand?",
  ];
}

/** A decision not to answer, shown as what it is: deliberate, and not an error (§14, LGL-006). */
function Declined({ kind, text }: { kind: "abstained" | "refused"; text: string }) {
  return (
    <div className="rounded-control border border-border px-3 py-2.5">
      <p className="text-label font-medium text-text">
        {kind === "abstained" ? "Not enough evidence to answer" : "Outside what Assay Desk covers"}
      </p>
      <p className="mt-1 text-sm text-muted">{text}</p>
    </div>
  );
}

function Answer({
  message,
  isLatest,
  onAsk,
}: {
  message: AssistantMessage;
  isLatest: boolean;
  onAsk: (question: string) => void;
}) {
  const waiting = !message.done && message.text === "";
  const declined = message.refused ? "refused" : message.abstained ? "abstained" : null;
  const references = message.citations.map((c) => ({
    number: c.number,
    sourceId: c.source_id,
    title: c.title ?? null,
    tier: c.tier ?? null,
  }));
  const trace = message.toolTrace ?? [];

  return (
    <div className="space-y-2">
      <AiMark label="Answer" />
      {waiting ? (
        <p className="flex items-center gap-2 text-sm text-muted">
          <Spinner label="Working on an answer" /> Checking the filings and figures…
        </p>
      ) : declined ? (
        <Declined kind={declined} text={message.text} />
      ) : (
        message.text && (
          <p className="whitespace-pre-wrap text-body leading-relaxed">
            <CitedText text={message.text} citations={message.citations} />
            {!message.done && <span aria-hidden className="ml-0.5 inline-block h-4 w-px animate-shimmer bg-text align-middle" />}
          </p>
        )
      )}

      {message.error && (
        <div role="alert" className="space-y-2 text-sm">
          <p className="text-negative">{message.error}</p>
          {message.question && isLatest && (
            <button
              type="button"
              onClick={() => onAsk(message.question ?? "")}
              className="font-medium text-primary underline underline-offset-2"
            >
              Ask again
            </button>
          )}
        </div>
      )}
      {message.stopped && <p className="text-caption text-subtle">Stopped.</p>}

      {message.done && !declined && !message.error && message.evidenceLabel === "limited_evidence" && (
        <Badge tone="warning">Limited evidence</Badge>
      )}

      {message.done && (trace.length > 0 || references.length > 0) && (
        <details className="group text-sm">
          <summary className="cursor-pointer list-none text-caption text-muted hover:text-text">
            <span className="underline decoration-border-strong decoration-dotted underline-offset-4">
              How this was answered
            </span>
          </summary>
          <div className="mt-2 space-y-3">
            {trace.length > 0 && (
              <ul className="space-y-0.5 text-caption text-muted">
                {trace.map((step, i) => (
                  <li key={i} className="flex justify-between gap-4">
                    <span>{step.tool.replace(/_/g, " ")}</span>
                    {step.latency_ms != null && <span className="tnum text-subtle">{step.latency_ms} ms</span>}
                  </li>
                ))}
              </ul>
            )}
            <ReferenceList entries={references} />
          </div>
        </details>
      )}

      {message.done && !declined && !message.error && message.messageId && (
        <Feedback path={`/chat/messages/${message.messageId}/feedback`} />
      )}

      {isLatest && message.done && (message.suggested?.length ?? 0) > 0 && (
        <ul className="flex flex-wrap gap-2 pt-1">
          {message.suggested?.map((question) => (
            <li key={question}>
              <button
                type="button"
                onClick={() => onAsk(question)}
                className="rounded-full border border-border-strong px-3 py-1 text-left text-label text-muted transition-colors hover:bg-surface-2 hover:text-text"
              >
                {question}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Composer({
  ticker,
  busy,
  onAsk,
  onStop,
  inputRef,
}: {
  ticker: string;
  busy: boolean;
  onAsk: (question: string) => void;
  onStop: () => void;
  inputRef: RefObject<HTMLTextAreaElement | null>;
}) {
  const [draft, setDraft] = useState("");

  // The panel sends focus here when it opens, but the first time it opens this component is
  // still on its way and focus lands on the panel's title instead. Take it from there.
  useEffect(() => {
    if (document.activeElement?.closest('[role="dialog"]')?.contains(inputRef.current)) inputRef.current?.focus();
  }, [inputRef]);

  // Grow with the text, up to a few lines, then scroll.
  useLayoutEffect(() => {
    const field = inputRef.current;
    if (!field) return;
    field.style.height = "auto";
    field.style.height = `${Math.min(field.scrollHeight, 160)}px`;
  }, [draft, inputRef]);

  const send = () => {
    if (busy || !draft.trim()) return;
    onAsk(draft);
    setDraft("");
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends; Shift+Enter is a new line. Enter that confirms an IME composition is neither.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send();
    }
  };

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        send();
      }}
      className="shrink-0 border-t border-border p-3"
    >
      <div className="flex items-end gap-2 rounded-surface border border-border-strong bg-surface-2 p-2 focus-within:border-primary">
        <textarea
          ref={inputRef}
          rows={1}
          value={draft}
          maxLength={MAX_QUESTION_LENGTH}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
          aria-label={`Ask a question about ${ticker}`}
          placeholder={`Ask about ${ticker}`}
          className="max-h-40 min-h-7 flex-1 resize-none bg-transparent px-1 py-1 text-sm text-text outline-none placeholder:text-subtle focus-visible:outline-none"
        />
        {busy ? (
          <IconButton size="sm" variant="secondary" label="Stop answering" onClick={onStop}>
            <Square aria-hidden className="size-3" fill="currentColor" />
          </IconButton>
        ) : (
          <IconButton size="sm" variant="primary" type="submit" label="Send question" disabled={!draft.trim()}>
            <ArrowUp aria-hidden className="size-4" />
          </IconButton>
        )}
      </div>
      <p className="mt-2 px-1 text-caption text-subtle">
        Answers come from this company’s filings and reported figures, with sources. Not investment
        advice.
      </p>
    </form>
  );
}

/** The assistant: questions about one company, answered from its filings and figures with
 *  citations (§10.14, FR-027), or declined when the evidence is not there. */
export function Assistant({
  ticker,
  conversation,
  composerRef,
}: {
  ticker: string;
  conversation: Conversation;
  composerRef: RefObject<HTMLTextAreaElement | null>;
}) {
  const { user, ready } = useAuth();
  const { messages, busy, ask, stop } = conversation;
  const scroller = useRef<HTMLDivElement>(null);
  const pinnedToEnd = useRef(true);
  const last = messages.at(-1);

  // Follow the answer as it streams, unless the reader has scrolled up to read something else.
  useEffect(() => {
    const element = scroller.current;
    if (element && pinnedToEnd.current) element.scrollTop = element.scrollHeight;
  }, [messages]);

  const onAsk = (question: string) => {
    pinnedToEnd.current = true;
    ask(question);
  };

  return (
    <>
      <div
        ref={scroller}
        onScroll={(event) => {
          const el = event.currentTarget;
          pinnedToEnd.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
        className="min-h-0 flex-1 space-y-6 overflow-y-auto p-5"
      >
        {messages.length === 0 ? (
          <div className="space-y-4">
            <p className="text-sm text-muted">
              Ask anything about {ticker}. Answers are drawn from its filings and reported figures,
              and each one shows its sources.
            </p>
            <ul aria-label="Suggested questions" className="divide-y divide-border/60 border-y border-border/60">
              {suggestedQuestions(ticker).map((question) => (
                <li key={question}>
                  {user ? (
                    <button
                      type="button"
                      onClick={() => onAsk(question)}
                      className="-mx-2 block w-[calc(100%+1rem)] rounded-control px-2 py-2.5 text-left text-sm transition-colors hover:bg-surface-2"
                    >
                      {question}
                    </button>
                  ) : (
                    <span className="block py-2.5 text-sm text-muted">{question}</span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ) : (
          messages.map((message) =>
            message.role === "user" ? (
              <p
                key={message.id}
                className="ml-auto w-fit max-w-[85%] whitespace-pre-wrap rounded-surface bg-surface-2 px-3.5 py-2 text-sm"
              >
                {message.text}
              </p>
            ) : (
              <Answer key={message.id} message={message} isLatest={message === last} onAsk={onAsk} />
            ),
          )
        )}
        {/* Announced once when an answer completes, not once per streamed word. */}
        <p role="status" className="sr-only">
          {last?.role === "assistant" && last.done ? "Answer ready." : ""}
        </p>
      </div>

      {!ready ? null : user ? (
        <Composer ticker={ticker} busy={busy} onAsk={onAsk} onStop={stop} inputRef={composerRef} />
      ) : (
        <div className="shrink-0 border-t border-border p-5">
          <p className="text-sm text-muted">Sign in to ask questions. It keeps your conversations yours.</p>
          <Link
            to="/login"
            state={{ from: `/company/${ticker}/chat` }}
            className="mt-3 inline-flex h-9 items-center rounded-control bg-primary px-3.5 text-sm font-medium text-primary-contrast transition-colors hover:bg-primary/90"
          >
            Sign in to ask
          </Link>
        </div>
      )}
    </>
  );
}
