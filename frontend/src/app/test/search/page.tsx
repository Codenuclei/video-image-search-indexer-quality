"use client";

import { SearchPage } from "@/components/views/search-view";
import { ReverseFaceLabPage } from "@/components/views/reverse-face-view";
import { DriveSessionBar } from "@/components/drive-session-bar";
import { TestIndexStatus } from "@/components/test-index-status";
import { useIndexStatusStore } from "@/lib/index-status-store";
import { formatCount } from "@/lib/index-errors";

export default function TestSearchPage() {
  const { status } = useIndexStatusStore();
  const processed = status?.counts_by_status.processed ?? 0;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Search and Index</h1>
          <p className="text-xs text-muted-foreground">
            All Photos{processed ? ` · ${formatCount(processed)} Assets` : ""}
          </p>
        </div>
        <div className="w-full max-w-sm">
          <TestIndexStatus compact />
        </div>
      </div>

      <DriveSessionBar />

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 space-y-6">
          <section className="rounded-2xl border border-border bg-card p-4 shadow-sm md:p-5">
            <SearchPage embedded hideSearchBar />
          </section>

          <section id="reverse-face" className="rounded-2xl border border-border bg-card p-4 shadow-sm md:p-5">
            <h2 className="mb-3 text-sm font-semibold uppercase tracking-[0.14em] text-muted-foreground">
              Reverse face
            </h2>
            <ReverseFaceLabPage embedded />
          </section>
        </div>

        <div className="space-y-4 xl:sticky xl:top-24 xl:self-start">
          <TestIndexStatus />
        </div>
      </div>
    </div>
  );
}
