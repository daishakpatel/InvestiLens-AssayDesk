// SSE client for POST /chat/stream. EventSource can't send an Authorization header, so we stream
// with fetch + a ReadableStream and parse the `token` → `citations` → `done` frames by hand
// (matches app/chat/streaming.py). The turn is verified server-side before streaming (ADR: tokens
// are the already-verified answer, not raw model output).
import { authorizedFetch } from "./apiClient";
import { API_BASE_URL } from "./config";
import type { ChatCitation, EvidenceLabel, ToolTraceEntry } from "../types";

export interface ChatDoneMeta {
  evidence_label: EvidenceLabel | null;
  abstained: boolean;
  refused: boolean;
  /** Abstained because the metric doesn't apply to the company's sector (DR-042). */
  not_applicable: boolean;
  session_id: string | number | null;
  message_id: string | number | null;
  suggested_questions: string[];
  tool_trace: ToolTraceEntry[];
}

export interface ChatStreamHandlers {
  onToken: (text: string) => void;
  onCitations: (citations: ChatCitation[]) => void;
  onDone: (meta: ChatDoneMeta) => void;
}

export interface ChatStreamBody {
  company: string;
  question: string;
  session_id?: string | null;
}

export async function streamChat(
  body: ChatStreamBody,
  handlers: ChatStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Request-ID": crypto.randomUUID(),
  };
  const res = await authorizedFetch(`${API_BASE_URL}/chat/stream`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    ...(signal ? { signal } : {}),
  });

  if (!res.ok || !res.body) {
    const detail = await res
      .json()
      .then((b: { detail?: string; title?: string }) => b.detail ?? b.title)
      .catch(() => undefined);
    throw new Error(detail ?? `Chat request failed (${res.status})`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      dispatch(frame, handlers);
    }
  }
}

function dispatch(frame: string, handlers: ChatStreamHandlers): void {
  let event = "message";
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return;
  const parsed: unknown = JSON.parse(data);
  if (event === "token") {
    handlers.onToken((parsed as { text: string }).text);
  } else if (event === "citations") {
    handlers.onCitations((parsed as { citations: ChatCitation[] }).citations);
  } else if (event === "done") {
    handlers.onDone(parsed as ChatDoneMeta);
  }
}
