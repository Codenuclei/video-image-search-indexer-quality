"use client";

import { useEffect } from "react";
import { SearchPage } from "@/components/views/search-view";
import { ImageSearchPanel } from "@/components/image-search-panel";
import { useIndexStatusStore } from "@/lib/index-status-store";
import { formatCount } from "@/lib/index-errors";
import {
  clearReverseFaceSearch,
  getReverseFaceSession,
} from "@/lib/reverse-face-session";

export default function TestSearchPage() {
  const { status } = useIndexStatusStore();
  const processed = status?.counts_by_status.processed ?? 0;

  // Fresh Search navigation should be idle until the user searches/uploads.
  // Keep leftover reverse-face state only for intentional handoffs (#reverse-face)
  // or an in-flight face search started just before landing here.
  useEffect(() => {
    const hash = typeof window !== "undefined" ? window.location.hash : "";
    const session = getReverseFaceSession();
    if (hash === "#reverse-face" || session.searching) return;
    if (session.result || session.file || session.previewUrl || session.tagMessage) {
      clearReverseFaceSearch();
    }
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Search and Index</h1>
        <p className="text-xs text-muted-foreground">
          All Photos{processed ? ` · ${formatCount(processed)} Assets` : ""}
        </p>
      </div>

      {/* Results only — header search bar is shown on this tab. */}
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
