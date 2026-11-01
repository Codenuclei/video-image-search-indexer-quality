"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, Rocket, ScanFace, Users } from "lucide-react";
import {
  apiClient,
  formatApiError,
  startupAssetUrl,
  type Startup,
} from "@/lib/api";
import { Card, LoadingLabel, ServiceErrorCard } from "@/components/ui";

function StartupLogo({ startup }: { startup: Startup }) {
  const src = startupAssetUrl(startup.logo_path);
  if (!src) {
    return (
      <div
        className="flex h-24 w-24 shrink-0 items-center justify-center rounded-xl bg-orange-500/10 text-orange-700 ring-1 ring-border dark:text-orange-300"
        aria-hidden
      >
        <Rocket size={32} />
      </div>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt={`${startup.name} logo`}
      className="h-24 w-24 shrink-0 rounded-xl object-contain ring-1 ring-border"
    />
  );
}

function clusterHref(cluster: Startup["face_clusters"][number]): string {
  if (cluster.person_id) return `/people/${cluster.person_id}`;
  return "/review";
}

export default function StartupDetailPage() {
  const params = useParams();
  const router = useRouter();
  const slug = typeof params.slug === "string" ? params.slug : "";
  const [startup, setStartup] = useState<Startup | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!slug) return;
    let cancelled = false;
    setStartup(null);
    setError(null);
    apiClient
      .getStartup(slug)
      .then((row) => {
        if (!cancelled) setStartup(row);
      })
      .catch((e) => {
        if (!cancelled) setError(formatApiError(e, "Failed to load startup"));
      });
    return () => {
      cancelled = true;
    };
  }, [slug]);

  if (error && !startup) {
    return (
      <div className="space-y-4">
        <button
          type="button"
          onClick={() => router.push("/startups")}
          className="inline-flex items-center gap-1.5 rounded-full border border-border bg-card px-3 py-1.5 text-xs font-medium text-muted-foreground shadow-sm transition-colors hover:bg-muted hover:text-foreground"
        >
          <ArrowLeft size={14} aria-hidden />
          Back
        </button>
        <ServiceErrorCard
          message={error}
          onRetry={() => {
            setError(null);
            void apiClient
              .getStartup(slug)
              .then(setStartup)
              .catch((e) => setError(formatApiError(e, "Failed to load startup")));
          }}
        />
      </div>
    );
  }

  if (!startup) {
    return (
      <p className="text-muted-foreground">
        <LoadingLabel size={16}>Loading…</LoadingLabel>
      </p>
    );
  }

  const aliases = startup.aliases.filter((alias) => alias.trim());

  return (
    <div className="min-w-0 max-w-full space-y-6 overflow-x-hidden">
      <button
        type="button"
        onClick={() => router.push("/startups")}
        className="inline-flex items-center gap-1.5 rounded-full border border-border bg-card px-3 py-1.5 text-xs font-medium text-muted-foreground shadow-sm transition-colors hover:bg-muted hover:text-foreground"
      >
        <ArrowLeft size={14} aria-hidden />
        Back
      </button>

      <div className="flex min-w-0 flex-col items-start gap-4 sm:flex-row sm:items-center">
        <StartupLogo startup={startup} />
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-xl font-semibold sm:text-2xl">{startup.name}</h2>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            {startup.cohort && (
              <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] font-semibold text-muted-foreground">
                {startup.cohort}
              </span>
            )}
            {startup.sector && (
              <span className="rounded-full bg-sky-500/15 px-2 py-0.5 text-[11px] font-semibold text-sky-700 dark:text-sky-300">
                {startup.sector}
              </span>
            )}
            <span className="inline-flex items-center gap-1 rounded-full bg-orange-500/15 px-2 py-0.5 text-[11px] font-semibold text-orange-700 dark:text-orange-300">
              <Users size={11} aria-hidden />
              {startup.founders.length} founder{startup.founders.length === 1 ? "" : "s"}
            </span>
          </div>
          {startup.one_liner && (
            <p className="mt-2 text-sm text-muted-foreground">{startup.one_liner}</p>
          )}
        </div>
      </div>

      {aliases.length > 0 && (
        <Card className="p-4">
          <h3 className="mb-2 text-sm font-medium">Aliases</h3>
          <div className="flex flex-wrap gap-1.5">
            {aliases.map((alias) => (
              <span
                key={alias}
                className="rounded-full border border-border bg-muted/60 px-2.5 py-0.5 text-xs text-foreground"
              >
                {alias}
              </span>
            ))}
          </div>
        </Card>
      )}

      <Card className="overflow-hidden p-0">
        <div className="border-b border-border px-4 py-3">
          <h3 className="font-medium">Founders</h3>
          <p className="text-xs text-muted-foreground">
            Catalog names and any linked people in the index
          </p>
        </div>
        {startup.founders.length === 0 ? (
          <p className="px-4 py-6 text-sm text-muted-foreground">No founders listed.</p>
        ) : (
          <ul className="divide-y divide-border">
            {startup.founders.map((founder) => {
              const label = founder.person_name || founder.founder_name;
              return (
                <li key={founder.id} className="flex items-center justify-between gap-3 px-4 py-3">
                  <div className="min-w-0">
                    {founder.person_id ? (
                      <Link
                        href={`/people/${founder.person_id}`}
                        className="truncate text-sm font-medium text-primary hover:underline"
                      >
                        {label}
                      </Link>
                    ) : (
                      <p className="truncate text-sm font-medium">{founder.founder_name}</p>
                    )}
                    {founder.person_name && founder.person_name !== founder.founder_name && (
                      <p className="truncate text-xs text-muted-foreground">
                        Catalog: {founder.founder_name}
                      </p>
                    )}
                  </div>
                  {founder.is_candidate && (
                    <span className="shrink-0 rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] font-semibold text-amber-800 dark:text-amber-200">
                      Candidate
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Card>

      <Card className="overflow-hidden p-0">
        <div className="border-b border-border px-4 py-3">
          <h3 className="font-medium">Linked face clusters</h3>
          <p className="text-xs text-muted-foreground">
            Named clusters open the person; unnamed ones go to the review queue
          </p>
        </div>
        {startup.face_clusters.length === 0 ? (
          <p className="px-4 py-6 text-sm text-muted-foreground">No face clusters linked yet.</p>
        ) : (
          <ul className="divide-y divide-border">
            {startup.face_clusters.map((cluster) => {
              const named = Boolean(cluster.person_id && cluster.person_name);
              const similarityPct =
                cluster.similarity != null ? Math.round(cluster.similarity * 100) : null;
              return (
                <li key={cluster.cluster_id}>
                  <Link
                    href={clusterHref(cluster)}
                    className="flex items-center gap-3 px-4 py-3 transition-colors hover:bg-muted/40"
                  >
                    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground">
                      <ScanFace size={16} aria-hidden />
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium">
                        {named ? cluster.person_name : `Cluster #${cluster.cluster_id}`}
                      </p>
                      <p className="truncate text-xs text-muted-foreground">
                        {cluster.member_count} face{cluster.member_count === 1 ? "" : "s"}
                        {cluster.status ? ` · ${cluster.status}` : ""}
                        {similarityPct != null ? ` · ${similarityPct}%` : ""}
                      </p>
                    </div>
                    <span className="shrink-0 text-xs font-medium text-primary">
                      {named ? "Person" : "Review"}
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </Card>
    </div>
  );
}
