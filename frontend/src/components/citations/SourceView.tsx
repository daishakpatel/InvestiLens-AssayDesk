import { ArrowUpRight } from "lucide-react";

import { formatValue } from "../../charts/series";
import { ApiError } from "../../lib/apiClient";
import { formatDate, toNumber } from "../../lib/format";
import { splitHighlight } from "../../lib/highlight";
import { metricDisplayByName, metricLabel } from "../../lib/metricCatalog";
import { useSource } from "../../lib/queries";
import { EXTERNAL_LINK, safeHttpUrl } from "../../lib/safeUrl";
import type { SourceDetail, SourceRecord } from "../../types";
import { CopyButton } from "../ui/CopyButton";
import { Skeleton } from "../ui/Skeleton";
import { ErrorState } from "../ui/States";
import { useCitations } from "./CitationContext";
import { sourceKindLabel, sourceTitle, tierLabel } from "./sourceTitle";

const LINK_BUTTON =
  "inline-flex h-7 items-center gap-1.5 rounded-control border border-border-strong px-2.5 text-label " +
  "font-medium transition-colors hover:bg-surface-2 active:bg-surface-3";

/** The cited passage, quoted, with the cited span marked. Filing and news text is untrusted
 *  content: it is rendered as text only, never as markup. */
function Passage({ detail }: { detail: SourceDetail }) {
  if (!detail.text) {
    return <p className="text-sm text-muted">No excerpt is available for this source.</p>;
  }
  const { before, marked, after } = splitHighlight(detail.text, detail.highlight_start, detail.highlight_end);
  return (
    <blockquote className="whitespace-pre-wrap border-l-2 border-border-strong pl-4 text-sm leading-relaxed text-text">
      {before}
      {marked && <mark className="rounded-[2px] bg-warning/25 text-text">{marked}</mark>}
      {after}
    </blockquote>
  );
}

function Derivation({
  source,
  lineage,
}: {
  source: Extract<SourceRecord, { source_type: "derived_metric" }>;
  lineage: Record<string, string>[];
}) {
  const { open } = useCitations();
  const value = toNumber(source.value ?? null);
  const display = metricDisplayByName(source.formula_id);
  const inputs = [...new Set(source.input_source_ids)];
  const rows: [string, string][] = [
    ["Formula", `${source.formula_id} (${source.formula_version})`],
    ...(value !== null
      ? [["Value", display ? formatValue(value, display) : String(source.value)] as [string, string]]
      : []),
    ...(source.period ? [["Period", source.period] as [string, string]] : []),
  ];
  return (
    <section aria-label="How this was calculated" className="space-y-3">
      <h4 className="text-label text-muted">How this was calculated</h4>
      <dl className="text-sm">
        {rows.map(([term, detail]) => (
          <div key={term} className="flex justify-between gap-6 border-b border-border/60 py-2">
            <dt className="text-muted">{term}</dt>
            <dd className="tnum text-right">{detail}</dd>
          </div>
        ))}
      </dl>
      {lineage.length > 0 && (
        <ul className="space-y-1 text-sm">
          {lineage.map((row, i) => (
            <li key={i} className="flex justify-between gap-4">
              <span>{row.name ? metricLabel(row.name) : "Input"}</span>
              {row.source_id && <span className="truncate text-subtle">{row.source_id}</span>}
            </li>
          ))}
        </ul>
      )}
      {inputs.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-label">
          <span className="text-muted">Inputs from</span>
          {inputs.map((id) => (
            <button key={id} type="button" onClick={() => open(id)} className={LINK_BUTTON}>
              {id}
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

/** One source, shown in the side panel: what it is, the passage itself, where it sits, and the
 *  way to the original (CIT-002, CIT-006). */
export function SourceView({ sourceId }: { sourceId: string }) {
  const query = useSource(sourceId);

  if (query.isPending) {
    return (
      <div role="status" aria-label="Loading source" className="space-y-3 p-5">
        <Skeleton className="w-2/5" />
        <Skeleton className="h-5 w-3/4" />
        <Skeleton shape="block" className="mt-4 h-40" />
      </div>
    );
  }
  if (query.isError) {
    const missing = query.error instanceof ApiError && query.error.status === 404;
    return missing ? (
      <p role="status" className="p-5 text-sm text-muted">
        This source is no longer available. It may belong to data that has since been re-ingested.
      </p>
    ) : (
      <ErrorState message="The source could not be loaded." onRetry={() => void query.refetch()} />
    );
  }

  const detail = query.data;
  const source = detail.source;
  const original = safeHttpUrl(detail.deep_link ?? source.url);
  const published = source.source_type === "news_item" ? source.published_at : null;
  const sectionPath =
    source.source_type === "text_chunk" && source.section_path.length > 0
      ? source.section_path.join(" › ")
      : null;
  const page = source.source_type === "text_chunk" ? source.page : null;
  const meta = [sourceKindLabel(source), tierLabel(source.tier), published && formatDate(published)].filter(Boolean);

  return (
    <div className="space-y-5 overflow-y-auto p-5">
      <header>
        <p className="text-caption text-muted">{meta.join(" · ")}</p>
        <h3 className="mt-1 text-title font-medium">{sourceTitle(source)}</h3>
      </header>

      <Passage detail={detail} />

      {(sectionPath || page != null) && (
        <p className="text-caption text-subtle">
          {[sectionPath && `Section: ${sectionPath}`, page != null && `Page ${page}`].filter(Boolean).join(" · ")}
        </p>
      )}

      {source.source_type === "derived_metric" && <Derivation source={source} lineage={detail.lineage} />}

      <div className="flex flex-wrap gap-2">
        {original && (
          <a href={original} {...EXTERNAL_LINK} className={LINK_BUTTON}>
            Open original
            <ArrowUpRight aria-hidden className="size-3.5" />
          </a>
        )}
        <CopyButton label="Copy citation" text={`${sourceTitle(source)} — ${original ?? source.source_id}`} />
      </div>
    </div>
  );
}
