import { useCallback, useEffect, useRef, useState } from "react";

import { streamChat } from "../../lib/chatStream";
import type { ChatCitation, EvidenceLabel, ToolTraceEntry } from "../../types";

export interface AssistantMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  citations: ChatCitation[];
  done: boolean;
  abstained?: boolean;
  refused?: boolean;
  notApplicable?: boolean;
  evidenceLabel?: EvidenceLabel | null;
  toolTrace?: ToolTraceEntry[];
  suggested?: string[];
  messageId?: string | null;
  /** The request failed; `text` may hold a partial answer. */
  error?: string;
  /** The reader stopped the answer before it finished. */
  stopped?: boolean;
  /** The question this answers, so a failed one can be asked again. */
  question?: string;
}

export interface Conversation {
  messages: AssistantMessage[];
  busy: boolean;
  ask: (question: string) => void;
  stop: () => void;
}

/** The longest question the composer accepts. A convenience, not the control: the API enforces
 *  the same limit (`MAX_QUESTION_LENGTH` in `backend/app/schemas/chat.py`). */
export const MAX_QUESTION_LENGTH = 2000;

/** One conversation about one company (§10.14, FR-027). It lives with the company page, not with
 *  the panel, so closing the panel or changing tab does not lose it; a different company gets a
 *  fresh one, so no context carries across companies. */
export function useConversation(ticker: string): Conversation {
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const sessionId = useRef<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  const inFlight = useRef(false);

  // Leaving the company page ends any answer still streaming.
  useEffect(() => () => controller.current?.abort(), []);

  const ask = useCallback(
    (question: string) => {
      const text = question.trim().slice(0, MAX_QUESTION_LENGTH);
      if (!text || inFlight.current) return;
      inFlight.current = true;
      setBusy(true);

      const answerId = crypto.randomUUID();
      const update = (patch: (message: AssistantMessage) => AssistantMessage) =>
        setMessages((all) => all.map((message) => (message.id === answerId ? patch(message) : message)));
      setMessages((all) => [
        ...all,
        { id: crypto.randomUUID(), role: "user", text, citations: [], done: true },
        { id: answerId, role: "assistant", text: "", citations: [], done: false, question: text },
      ]);

      const abort = new AbortController();
      controller.current = abort;
      streamChat(
        { company: ticker, question: text, session_id: sessionId.current },
        {
          onToken: (token) => update((m) => ({ ...m, text: m.text + token })),
          onCitations: (citations) => update((m) => ({ ...m, citations })),
          onDone: (meta) => {
            update((m) => ({
              ...m,
              done: true,
              abstained: meta.abstained,
              refused: meta.refused,
              notApplicable: meta.not_applicable,
              evidenceLabel: meta.evidence_label,
              toolTrace: meta.tool_trace,
              suggested: meta.suggested_questions,
              messageId: meta.message_id != null ? String(meta.message_id) : null,
            }));
            if (meta.session_id != null) sessionId.current = String(meta.session_id);
          },
        },
        abort.signal,
      )
        .catch((error: unknown) => {
          if (abort.signal.aborted) {
            update((m) => ({ ...m, done: true, stopped: true }));
          } else {
            const message = error instanceof Error ? error.message : "Something went wrong.";
            update((m) => ({ ...m, done: true, error: message }));
          }
        })
        .finally(() => {
          // A stream that ends without a "done" frame must not leave the answer spinning.
          update((m) => (m.done ? m : { ...m, done: true }));
          inFlight.current = false;
          setBusy(false);
        });
    },
    [ticker],
  );

  const stop = useCallback(() => controller.current?.abort(), []);

  return { messages, busy, ask, stop };
}
