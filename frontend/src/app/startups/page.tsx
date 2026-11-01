"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Rocket, Users } from "lucide-react";
import {
  apiClient,
  formatApiError,
  startupAssetUrl,
  type Startup,
  type StartupListResponse,
} from "@/lib/api";
import { Button, Card, LoadingLabel, ServiceErrorCard } from "@/components/ui";

function StartupLogo({ startup, className }: { startup: Startup; className?: string }) {
  const src = startupAssetUrl(startup.logo_path);
  if (!src) {
    return (
      <div
        className={`flex items-center justify-center bg-orange-500/10 text-orange-700 dark:text-orange-300 ${className ?? ""}`}
        aria-hidden
      >
        <Rocket size={22} />
      </div>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img src={src} alt="" className={className} />
  );
}

export default function StartupsPage() {
  const [data, setData] = useState<StartupListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await apiClient.listStartups());
    } catch (e) {
      setError(formatApiError(e, "Failed to load startups"));
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const items = data?.items ?? [];

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold sm:text-2xl">Startups</h2>
        <p className="text-sm text-muted-foreground">
          Catalog companies with linked founder faces and logo matches.
        </p>
      </div>

      {loading && (
        <p className="text-sm text-muted-foreground">
          <LoadingLabel size={16}>Loading startups…</LoadingLabel>
        </p>
      )}

      {error && !loading && items.length === 0 && (
        <ServiceErrorCard message={error} onRetry={() => void load()} />
      )}

      {!loading && !error && items.length === 0 && (
        <Card>
          <div className="flex flex-col items-center gap-2 px-2 py-6 text-center">
            <Rocket className="text-muted-foreground" size={28} aria-hidden />
            <p className="text-sm text-muted-foreground">
              {data?.catalog_present
                ? "No startups indexed yet."
                : "Startup catalog is not loaded yet."}
            </p>
          </div>
        </Card>
      )}

      {items.length > 0 && (
        <ul className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,17.5rem),1fr))] gap-4">
          {items.map((startup) => {
            const founderCount = startup.founders.length;
            return (
              <li key={startup.id}>
                <Link href={`/startups/${startup.slug}`} className="block h-full">
                  <Card className="flex h-full flex-col gap-3 p-4 transition-all duration-200 hover:-translate-y-0.5 hover:shadow-[0_12px_40px_-18px_rgba(0,0,0,0.35)]">
                    <div className="flex items-start gap-3">
                      <StartupLogo
                        startup={startup}
                        className="h-14 w-14 shrink-0 rounded-xl object-contain ring-1 ring-border"
                      />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-[15px] font-semibold tracking-tight">
                          {startup.name}
                        </p>
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
                        </div>
                      </div>
                    </div>
                    <p className="mt-auto inline-flex items-center gap-1 text-xs text-muted-foreground">
                      <Users size={12} aria-hidden />
                      {founderCount} founder{founderCount === 1 ? "" : "s"}
                    </p>
                  </Card>
                </Link>
              </li>
            );
          })}
        </ul>
      )}

      {error && items.length > 0 && (
        <div className="flex justify-center">
          <Button variant="secondary" onClick={() => void load()}>
            Retry
          </Button>
        </div>
      )}
    </div>
  );
}
