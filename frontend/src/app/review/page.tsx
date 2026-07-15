"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiClient, type Cluster, type Person } from "@/lib/api";
import { Button, Card, FaceThumb, Input, ServiceErrorCard } from "@/components/ui";
import { PersonMergeSearch } from "@/components/person-merge-search";

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
  const namingRef = useRef(false);

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
    const t = setTimeout(() => setSuccessMessage(null), 8000);
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
    try {
      await apiClient.nameCluster(id, name);
      setNames((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
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
    try {
      await apiClient.ignoreCluster(id);
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
    try {
      const updated = await apiClient.mergeCluster(id, person.id);
      setSuccessMessage(
        `Merged ${cluster.member_count} face${cluster.member_count === 1 ? "" : "s"} from cluster #${id} into ${updated.name} — ${updated.name} now has ${updated.occurrence_count} appearance${updated.occurrence_count === 1 ? "" : "s"}.`
      );
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
        <Card className="border-green-800 bg-green-950/30 text-green-200">{successMessage}</Card>
      )}
      {error && (
        <ServiceErrorCard message={error} onRetry={() => load()} onDismiss={() => setError(null)} />
      )}
      {initialLoading && <p className="text-muted-foreground">Loading review queue…</p>}
      {refreshing && !initialLoading && (
        <p className="text-xs text-muted-foreground">Refreshing queue…</p>
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

          return (
            <Card key={c.id} className="space-y-3">
              <div className="flex gap-3">
                <FaceThumb faceId={c.representative_face_id} className="h-20 w-20 shrink-0 rounded-lg" />
                <div className="min-w-0">
                  <p className="font-medium">Cluster #{c.id}</p>
                  <p className="text-sm text-muted-foreground">{c.member_count} face(s)</p>
                  {c.representative_confidence != null && (
                    <p className="text-xs text-muted-foreground">{(c.representative_confidence * 100).toFixed(0)}% confidence</p>
                  )}
                  {c.appears_in.length > 0 && (
                    <p className="text-xs text-muted-foreground">
                      In {c.appears_in.length} file{c.appears_in.length === 1 ? "" : "s"}
                    </p>
                  )}
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
                  {naming ? "Naming…" : "Name"}
                </Button>
              </div>
              <div className="flex flex-wrap items-start gap-2">
                <Button variant="secondary" disabled={busy} onClick={() => ignoreCluster(c.id)}>
                  {ignoring ? "Ignoring…" : "Ignore"}
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
