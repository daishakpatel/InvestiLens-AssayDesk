import { Check, Copy } from "lucide-react";
import { useState } from "react";

import { Button } from "./Button";

/** Copies `text` and says so for two seconds; a denied clipboard claims nothing. */
export function CopyButton({
  text,
  label,
  variant = "secondary",
}: {
  text: string;
  label: string;
  variant?: "secondary" | "ghost";
}) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access denied or unavailable: nothing was copied, so claim nothing.
    }
  };
  return (
    <Button variant={variant} size="sm" onClick={() => void copy()}>
      {copied ? <Check aria-hidden className="size-3.5" /> : <Copy aria-hidden className="size-3.5" />}
      <span aria-live="polite">{copied ? "Copied" : label}</span>
    </Button>
  );
}
