"use client";

import { SearchPage } from "@/components/views/search-view";
import { ImageSearchPanel } from "@/components/image-search-panel";
import { useIndexStatusStore } from "@/lib/index-status-store";
import { formatCount } from "@/lib/index-errors";

export default function TestSearchPage() {
  const { status } = useIndexStatusStore();
  const processed = status?.counts_by_status.processed ?? 0;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Search and Index</h1>
        <p className="text-xs text-muted-foreground">
          All Photos{processed ? ` · ${formatCount(processed)} Assets` : ""}
        </p>
      </div>

      {/* Results only — no empty card chrome when the header search bar is used. */}
      <SearchPage embedded hideSearchBar />

      <section id="reverse-face" className="rounded-2xl border border-border bg-card p-4 shadow-sm md:p-5">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-[0.14em] text-muted-foreground">
          Search by image
        </h2>
        <ImageSearchPanel />
      </section>
    </div>
  );
}
