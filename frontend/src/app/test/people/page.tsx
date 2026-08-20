"use client";

import { Suspense, useMemo } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { PeoplePage } from "@/components/views/people-view";
import { ReviewPage } from "@/components/views/review-view";
import { ReverseFaceLabPage } from "@/components/views/reverse-face-view";
import { TestIndexStatus } from "@/components/test-index-status";
import { LoadingLabel } from "@/components/ui";
import { cn } from "@/lib/utils";

const tabs = [
  { id: "indexed", label: "Indexed People" },
  { id: "mu", label: "MU People" },
  { id: "unindexed", label: "Un-Indexed People" },
] as const;

type PeopleTab = (typeof tabs)[number]["id"];

function PeopleDirectory() {
  const router = useRouter();
  const params = useSearchParams();
  const tab = useMemo<PeopleTab>(() => {
    const raw = params.get("tab");
    if (raw === "mu" || raw === "unindexed" || raw === "indexed") return raw;
    return "indexed";
  }, [params]);

  function setTab(next: PeopleTab) {
    const query = next === "indexed" ? "" : `?tab=${next}`;
    router.replace(`/test/people${query}`);
  }

  return (
    <div className="space-y-6">
      <div>
        <p className="text-xs text-muted-foreground">Library · People Directory</p>
        <h1 className="text-2xl font-semibold tracking-tight">People Directory</h1>
      </div>

      <div className="flex flex-wrap gap-2">
        {tabs.map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => setTab(item.id)}
            className={cn(
              "rounded-full px-4 py-2 text-sm font-medium transition-colors",
              tab === item.id
                ? "bg-blue-600 text-white shadow-sm"
                : "bg-card text-muted-foreground ring-1 ring-border transition-colors hover:bg-accent hover:text-accent-foreground"
            )}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 rounded-2xl border border-border bg-card p-4 shadow-sm md:p-6">
          {tab === "indexed" && <PeoplePage embedded />}
          {tab === "mu" && <ReverseFaceLabPage embedded />}
          {tab === "unindexed" && <ReviewPage embedded />}
        </div>
        <div className="space-y-4 xl:sticky xl:top-24 xl:self-start">
          <TestIndexStatus />
        </div>
      </div>
    </div>
  );
}

export default function TestPeopleDirectoryPage() {
  return (
    <Suspense
      fallback={
        <p className="text-sm text-muted-foreground">
          <LoadingLabel>Loading people directory…</LoadingLabel>
        </p>
      }
    >
      <PeopleDirectory />
    </Suspense>
  );
}
