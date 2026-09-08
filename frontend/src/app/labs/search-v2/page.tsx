"use client";

import { FormEvent, useState } from "react";
import Link from "next/link";
import { ArrowLeft, Search } from "lucide-react";
import {
  apiClient,
  driveFileThumbnailUrl,
  formatApiError,
  type ObjectEvidence,
  type SearchResultFile,
} from "@/lib/api";
import { Button, Input, LoadingLabel } from "@/components/ui";

function ObjectChips({ items }: { items?: ObjectEvidence[] }) {
  if (!items?.length) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {items.map((item) => {
        const action = item.category === "action" || item.source === "qwen_action";
        return (
          <span
            key={`${item.label}:${item.source}`}
            title={`${item.source} · ${item.category}`}
            className={
              action
                ? "rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[10px] font-medium text-amber-800 dark:text-amber-300"
                : "rounded-full border border-violet-500/30 bg-violet-500/10 px-2 py-0.5 text-[10px] font-medium text-violet-700 dark:text-violet-300"
            }
          >
            {item.label}
          </span>
        );
      })}
    </div>
  );
}

export default function SearchDemoV2Page() {
  const [query, setQuery] = useState("giving cheque");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [files, setFiles] = useState<SearchResultFile[]>([]);
  const [ran, setRan] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const q = query.trim();
    if (!q) return;
    setLoading(true);
    setError(null);
    try {
      const result = await apiClient.searchTestV2(q, "image");
      setFiles(result.files ?? []);
      setRan(true);
    } catch (err) {
      setError(formatApiError(err, "Search demo failed"));
      setFiles([]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 md:p-6">
      <Link
        href="/search"
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft size={14} />
        Production search
      </Link>
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Search demo v2</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          Isolated from production. Caption, visual, and Qwen evidence are a
          union ranked by overlap — no student/LLM/object hard filters. Production{" "}
          <code>/search</code> is unchanged.
        </p>
      </div>
      <form onSubmit={onSubmit} className="flex flex-col gap-2 sm:flex-row">
        <Input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="giving cheque, students cooking, hyrox delhi…"
          className="flex-1"
        />
        <Button type="submit" disabled={loading}>
          <Search size={16} />
          {loading ? "Searching…" : "Try demo"}
        </Button>
      </form>
      {loading && <LoadingLabel>Running /search/testv2</LoadingLabel>}
      {error && <p className="text-sm text-red-600">{error}</p>}
      {ran && !loading && (
        <p className="text-xs text-muted-foreground">{files.length} image(s)</p>
      )}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4">
        {files.map((file) => (
          <article
            key={file.drive_file_id}
            className="overflow-hidden rounded-xl border border-border bg-card"
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={driveFileThumbnailUrl(file.drive_file_id)}
              alt={file.name}
              className="aspect-square w-full object-cover"
            />
            <div className="space-y-1 p-2">
              <p className="truncate text-xs font-medium">{file.name}</p>
              {file.caption ? (
                <p className="line-clamp-3 text-[11px] leading-snug text-muted-foreground">
                  {file.caption}
                </p>
              ) : null}
              <ObjectChips items={file.matched_objects} />
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}
