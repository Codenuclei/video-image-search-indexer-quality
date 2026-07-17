"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { FileImage, Gauge, ScanFace, Tag, Users } from "lucide-react";
import { apiClient, type Cluster, type Person } from "@/lib/api";
import { Button, Card, FaceThumb, Input, LoadingLabel, ServiceErrorCard } from "@/components/ui";
import { PersonMergeSearch } from "@/components/person-merge-search";
import { AnimatedTrash } from "@/components/animated-trash";
import { cn } from "@/lib/utils";

function confidenceBadgeClass(pct: number) {
  if (pct >= 90) return "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300";
  if (pct >= 70) return "bg-amber-500/15 text-amber-700 dark:text-amber-300";
  return "bg-rose-500/15 text-rose-700 dark:text-rose-300";
}

const CLUSTER_PAGE_SIZE = 100;

type ClusterAction =
  | { type: "name"; clusterId: number }
  | { type: "ignore"; clusterId: number }
  | { type: "merge"; clusterId: number; personName: string };

export default function ReviewPage() {
  const [clusters, setClusters] = useState<Cluster[]>([]);
  const [clusterOffset, setClusterOffset] = useState(0);
  const [clusterTotal, setClusterTotal] = useState(0);
  const [names, setNames] = useState<Record<number, string>>({});
  const [initialLoading, setInitialLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [action, setAction] = useState<ClusterAction | null>(null);
  const [vanishingIds, setVanishingIds] = useState<Set<number>>(new Set());
  const namingRef = useRef(false);

  /** Play the card exit animation before the queue refresh removes the card. */
  async function vanishCard(id: number) {
    setVanishingIds((prev) => new Set(prev).add(id));
    await new Promise((resolve) => setTimeout(resolve, 420));
    setVanishingIds((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }

  const load = useCallback(
    async ({ initial = false, offset = clusterOffset }: { initial?: boolean; offset?: number } = {}) => {
      if (initial) setInitialLoading(true);
      else setRefreshing(true);
      try {
        const response = await apiClient.clusters({ limit: CLUSTER_PAGE_SIZE, offset });
        setClusters(response.items);
        setClusterTotal(response.total);
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load");
      } finally {
        if (initial) setInitialLoading(false);
        else setRefreshing(false);
      }
    },
    [clusterOffset]
  );

  useEffect(() => {
    load({ initial: clusterOffset === 0 });
  }, [clusterOffset, load]);

  useEffect(() => {
    if (!successMessage) return;
    const t = setTimeout(() => setSuccessMessage(null), 4500);
    return () => clearTimeout(t);
  }, [successMessage]);

  function isBusy(clusterId: number) {
    return action?.clusterId === clusterId;
  }

  async function nameCluster(id: number) {
    if (namingRef.current || action) return;
    const name = names[id]?.trim();
    if (!name) return;
    namingRef.current = true;
    setAction({ type: "name", clusterId: id });
    setError(null);
    setSuccessMessage(null);
    try {
      await apiClient.nameCluster(id, name);
      setNames((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      setSuccessMessage(`Named cluster #${id} as “${name}”. Future matches will auto-tag.`);
      await vanishCard(id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to name cluster");
    } finally {
      namingRef.current = false;
      setAction(null);
    }
  }

  async function ignoreCluster(id: number) {
    if (action) return;
    setAction({ type: "ignore", clusterId: id });
    setError(null);
    setSuccessMessage(null);
    try {
      await apiClient.ignoreCluster(id);
      setSuccessMessage(`Ignored cluster #${id}. It won’t appear in the review queue.`);
      await vanishCard(id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to ignore cluster");
    } finally {
      setAction(null);
    }
  }

  async function mergeCluster(id: number, person: Person) {
    if (action) return;
    const cluster = clusters.find((c) => c.id === id);
    if (!cluster) return;

    setAction({ type: "merge", clusterId: id, personName: person.name });
    setError(null);
    setSuccessMessage(null);
    try {
      const updated = await apiClient.mergeCluster(id, person.id);
      setSuccessMessage(
        `Merged ${cluster.member_count} face${cluster.member_count === 1 ? "" : "s"} from cluster #${id} into ${updated.name} — now ${updated.occurrence_count} appearance${updated.occurrence_count === 1 ? "" : "s"}.`
      );
      await vanishCard(id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to merge cluster");
    } finally {
      setAction(null);
    }
  }

  const pageStart = clusterTotal === 0 ? 0 : clusterOffset + 1;
  const pageEnd = Math.min(clusterOffset + clusters.length, clusterTotal);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-semibold">Unknown Faces</h2>
        <p className="text-sm text-muted-foreground">
          Name a person once — future appearances auto-tag. Only faces with 80%+ detection confidence appear here.
        </p>
      </div>

      {successMessage && (
        <div
          role="status"
          className="fixed inset-x-4 top-4 z-50 mx-auto flex max-w-lg items-start gap-3 rounded-xl border border-emerald-500/40 bg-emerald-50 px-4 py-3 text-sm text-emerald-950 shadow-lg shadow-emerald-950/10 dark:border-emerald-400/30 dark:bg-emerald-950 dark:text-emerald-50 md:inset-x-auto md:right-8 md:top-8"
        >
          <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-emerald-500 text-[11px] font-bold text-white">
            ✓
          </span>
          <p className="min-w-0 flex-1 font-medium leading-snug">{successMessage}</p>
          <button
            type="button"
            aria-label="Dismiss"
            className="shrink-0 rounded-md px-1.5 py-0.5 text-xs font-medium text-emerald-800/70 hover:bg-emerald-500/15 hover:text-emerald-950 dark:text-emerald-100/70 dark:hover:text-emerald-50"
            onClick={() => setSuccessMessage(null)}
          >
            ✕
          </button>
        </div>
      )}
      {error && (
        <ServiceErrorCard message={error} onRetry={() => load()} onDismiss={() => setError(null)} />
      )}
      {initialLoading && (
        <p className="text-muted-foreground">
          <LoadingLabel size={16}>Loading review queue…</LoadingLabel>
        </p>
      )}
      {refreshing && !initialLoading && (
        <p className="pointer-events-none sticky top-0 z-10 mb-1 w-fit rounded-md border border-border bg-background/90 px-2 py-1 text-xs text-muted-foreground shadow-sm backdrop-blur">
          <LoadingLabel size={12}>Updating queue…</LoadingLabel>
        </p>
      )}

      {!initialLoading && clusters.length === 0 && (
        <Card>
          <p className="text-muted-foreground">No unknown faces in the review queue. Index some images to get started.</p>
        </Card>
      )}

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {clusters.map((c) => {
          const busy = isBusy(c.id);
          const naming = action?.type === "name" && action.clusterId === c.id;
          const ignoring = action?.type === "ignore" && action.clusterId === c.id;
          const merging = action?.type === "merge" && action.clusterId === c.id;
          const mergeBusyLabel =
            merging && action?.type === "merge" ? `Merging into ${action.personName}…` : "Merging…";

          const confidencePct =
            c.representative_confidence != null ? Math.round(c.representative_confidence * 100) : null;

          return (
            <Card
              key={c.id}
              className={cn(
                "space-y-3 transition-shadow hover:shadow-md",
                vanishingIds.has(c.id) && "card-vanish"
              )}
            >
              <div className="flex gap-3">
                <FaceThumb
                  faceId={c.representative_face_id}
                  className="h-20 w-20 shrink-0 rounded-lg ring-2 ring-amber-400/30"
                />
                <div className="min-w-0 flex-1">
                  <p className="flex items-center gap-1.5 font-medium">
                    <ScanFace size={16} aria-hidden className="shrink-0 text-amber-500" />
                    Cluster #{c.id}
                  </p>
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                    <span className="inline-flex items-center gap-1 rounded-full bg-sky-500/15 px-2 py-0.5 text-[11px] font-semibold text-sky-700 dark:text-sky-300">
                      <Users size={11} aria-hidden />
                      {c.member_count} face{c.member_count === 1 ? "" : "s"}
                    </span>
                    {confidencePct != null && (
                      <span
                        className={cn(
                          "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold",
                          confidenceBadgeClass(confidencePct)
                        )}
                      >
                        <Gauge size={11} aria-hidden />
                        {confidencePct}%
                      </span>
                    )}
                    {c.appears_in.length > 0 && (
                      <span className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-[11px] font-semibold text-muted-foreground">
                        <FileImage size={11} aria-hidden />
                        {c.appears_in.length} file{c.appears_in.length === 1 ? "" : "s"}
                      </span>
                    )}
                  </div>
                </div>
              </div>
              <div className="flex flex-col gap-2 sm:flex-row">
                <Input
                  className="flex-1"
                  placeholder="Enter name..."
                  value={names[c.id] ?? ""}
                  disabled={busy}
                  onChange={(e) => setNames((n) => ({ ...n, [c.id]: e.target.value }))}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      nameCluster(c.id);
                    }
                  }}
                />
                <Button
                  className="shrink-0"
                  disabled={busy || !names[c.id]?.trim()}
                  onClick={() => nameCluster(c.id)}
                >
                  {naming ? (
                    <LoadingLabel>Naming…</LoadingLabel>
                  ) : (
                    <span className="inline-flex items-center gap-1.5">
                      <Tag size={14} aria-hidden />
                      Name
                    </span>
                  )}
                </Button>
              </div>
              <div className="flex flex-wrap items-start gap-2 border-t border-border pt-3">
                <Button
                  variant="secondary"
                  className="group/trash shrink-0 hover:border-destructive/40 hover:text-destructive"
                  disabled={busy}
                  onClick={() => ignoreCluster(c.id)}
                >
                  {ignoring ? (
                    <span className="inline-flex items-center gap-1.5">
                      <AnimatedTrash size={14} animating />
                      Ignoring…
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1.5">
                      <AnimatedTrash size={14} />
                      Ignore
                    </span>
                  )}
                </Button>
                <PersonMergeSearch
                  disabled={busy}
                  busy={merging}
                  busyLabel={mergeBusyLabel}
                  onSelect={(person) => mergeCluster(c.id, person)}
                />
              </div>
            </Card>
          );
        })}
      </div>

      {clusterTotal > CLUSTER_PAGE_SIZE && (
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-sm text-muted-foreground">
            Showing {pageStart}–{pageEnd} of {clusterTotal} unknown faces
          </p>
          <div className="flex gap-2">
            <Button
              variant="secondary"
              disabled={clusterOffset === 0 || refreshing}
              onClick={() => setClusterOffset((prev) => Math.max(0, prev - CLUSTER_PAGE_SIZE))}
            >
              Previous
            </Button>
            <Button
              variant="secondary"
              disabled={clusterOffset + CLUSTER_PAGE_SIZE >= clusterTotal || refreshing}
              onClick={() => setClusterOffset((prev) => prev + CLUSTER_PAGE_SIZE)}
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
