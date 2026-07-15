"use client";

import { useEffect, useRef, useState } from "react";
import { apiClient, type Cluster, type Person } from "@/lib/api";
import { Button, Card, FaceThumb, Input } from "@/components/ui";

type ClusterAction =
  | { type: "name"; clusterId: number }
  | { type: "ignore"; clusterId: number }
  | { type: "merge"; clusterId: number; personName: string };

export default function ReviewPage() {
  const [clusters, setClusters] = useState<Cluster[]>([]);
  const [persons, setPersons] = useState<Person[]>([]);
  const [names, setNames] = useState<Record<number, string>>({});
  const [mergeSelections, setMergeSelections] = useState<Record<number, string>>({});
  const [initialLoading, setInitialLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [action, setAction] = useState<ClusterAction | null>(null);
  const namingRef = useRef(false);

  async function load({ initial = false }: { initial?: boolean } = {}) {
    if (initial) setInitialLoading(true);
    else setRefreshing(true);
    try {
      const [c, p] = await Promise.all([apiClient.clusters(), apiClient.persons()]);
      setClusters(c);
      setPersons(p);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      if (initial) setInitialLoading(false);
      else setRefreshing(false);
    }
  }

  useEffect(() => {
    load({ initial: true });
  }, []);

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

  async function mergeCluster(id: number, personId: number) {
    if (action) return;
    const cluster = clusters.find((c) => c.id === id);
    const person = persons.find((p) => p.id === personId);
    if (!cluster || !person) return;

    setAction({ type: "merge", clusterId: id, personName: person.name });
    setError(null);
    try {
      const updated = await apiClient.mergeCluster(id, personId);
      setSuccessMessage(
        `Merged ${cluster.member_count} face${cluster.member_count === 1 ? "" : "s"} from cluster #${id} into ${updated.name} — ${updated.name} now has ${updated.occurrence_count} appearance${updated.occurrence_count === 1 ? "" : "s"}.`
      );
      setMergeSelections((prev) => ({ ...prev, [id]: "" }));
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to merge cluster");
    } finally {
      setAction(null);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-semibold">Unknown Faces</h2>
        <p className="text-sm text-zinc-400">
          Name a person once — future appearances auto-tag. Only faces with 80%+ detection confidence appear here.
        </p>
      </div>

      {successMessage && (
        <Card className="border-green-800 bg-green-950/30 text-green-200">{successMessage}</Card>
      )}
      {error && <Card className="border-red-800 text-red-300">{error}</Card>}
      {initialLoading && <p className="text-zinc-400">Loading review queue…</p>}
      {refreshing && !initialLoading && (
        <p className="text-xs text-zinc-500">Refreshing queue…</p>
      )}

      {!initialLoading && clusters.length === 0 && (
        <Card>
          <p className="text-zinc-400">No unknown faces in the review queue. Index some images to get started.</p>
        </Card>
      )}

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {clusters.map((c) => {
          const busy = isBusy(c.id);
          const naming = action?.type === "name" && action.clusterId === c.id;
          const ignoring = action?.type === "ignore" && action.clusterId === c.id;
          const merging = action?.type === "merge" && action.clusterId === c.id;
          const mergeLabel =
            merging && action?.type === "merge" ? `Merging into ${action.personName}…` : "Merge into…";

          return (
            <Card key={c.id} className="space-y-3">
              <div className="flex gap-3">
                <FaceThumb faceId={c.representative_face_id} className="h-20 w-20 shrink-0 rounded-lg" />
                <div className="min-w-0">
                  <p className="font-medium">Cluster #{c.id}</p>
                  <p className="text-sm text-zinc-400">{c.member_count} face(s)</p>
                  {c.representative_confidence != null && (
                    <p className="text-xs text-zinc-500">{(c.representative_confidence * 100).toFixed(0)}% confidence</p>
                  )}
                  {c.appears_in.length > 0 && (
                    <p className="text-xs text-zinc-500">
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
              <div className="flex flex-wrap gap-2">
                <Button variant="secondary" disabled={busy} onClick={() => ignoreCluster(c.id)}>
                  {ignoring ? "Ignoring…" : "Ignore"}
                </Button>
                {persons.length > 0 && (
                  <select
                    className="rounded-md border border-border bg-background px-2 py-1 text-sm disabled:opacity-50"
                    value={mergeSelections[c.id] ?? ""}
                    disabled={busy}
                    onChange={(e) => {
                      const personId = Number(e.target.value);
                      if (!personId) return;
                      setMergeSelections((prev) => ({ ...prev, [c.id]: e.target.value }));
                      mergeCluster(c.id, personId);
                    }}
                  >
                    <option value="" disabled>
                      {mergeLabel}
                    </option>
                    {persons.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                )}
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
