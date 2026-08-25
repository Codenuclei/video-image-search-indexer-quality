"use client";

import { Suspense, useMemo } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { PeoplePage } from "@/components/views/people-view";
import { ReviewPage } from "@/components/views/review-view";
import { ReverseFaceLabPage } from "@/components/views/reverse-face-view";
import { LoadingLabel } from "@/components/ui";
import { cn } from "@/lib/utils";
import { useRegisterTestShellChrome } from "@/lib/test-shell-chrome";

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

  useRegisterTestShellChrome(
    <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center sm:gap-4">
      <h1 className="shrink-0 text-lg font-semibold tracking-tight sm:text-xl">People Directory</h1>
      <div className="flex min-w-0 flex-wrap gap-1.5">
        {tabs.map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => setTab(item.id)}
            className={cn(
              "rounded-full px-3 py-1.5 text-xs font-medium transition-colors sm:px-4 sm:text-sm",
              tab === item.id
                ? "bg-blue-600 text-white shadow-sm"
                : "bg-muted text-muted-foreground ring-1 ring-border transition-colors hover:bg-accent hover:text-accent-foreground"
            )}
          >
            {item.label}
          </button>
        ))}
      </div>
    </div>,
    [tab]
  );

  return (
    <div>
      {tab === "indexed" && <PeoplePage embedded personHref={(id) => `/test/people/${id}`} />}
      {tab === "mu" && <ReverseFaceLabPage embedded />}
      {tab === "unindexed" && <ReviewPage embedded />}
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
