import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";

import { apiRequest } from "../../lib/apiClient";
import { IconButton } from "./Button";

/** Thumbs up/down, kept for evaluation (spec §18.5). `body` carries what the rating is about
 *  when the path does not (a report id, a claim id). */
export function Feedback({
  path,
  body = {},
}: {
  path: string;
  body?: Record<string, string>;
}) {
  const [sent, setSent] = useState<"up" | "down" | null>(null);
  const send = async (rating: "up" | "down") => {
    setSent(rating);
    try {
      await apiRequest(path, { method: "POST", body: { ...body, rating } });
    } catch {
      setSent(null); // not recorded: let the reader try again rather than thank them for nothing
    }
  };
  return (
    <div className="flex items-center gap-0.5">
      <IconButton
        size="sm"
        label="Helpful"
        aria-pressed={sent === "up"}
        onClick={() => void send("up")}
        className={sent === "up" ? "text-text" : ""}
      >
        <ThumbsUp aria-hidden className="size-3.5" />
      </IconButton>
      <IconButton
        size="sm"
        label="Not helpful"
        aria-pressed={sent === "down"}
        onClick={() => void send("down")}
        className={sent === "down" ? "text-text" : ""}
      >
        <ThumbsDown aria-hidden className="size-3.5" />
      </IconButton>
      {sent && (
        <span role="status" className="ml-1 text-caption text-subtle">
          Thanks for the feedback.
        </span>
      )}
    </div>
  );
}
