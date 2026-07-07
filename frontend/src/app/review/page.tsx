"use client";

import { useEffect, useState } from "react";
import { apiClient, type Cluster, type Person } from "@/lib/api";
import { Button, Card, FaceThumb, Input } from "@/components/ui";

export default function ReviewPage() {
  const [clusters, setClusters] = useState<Cluster[]>([]);
  const [persons, setPersons] = useState<Person[]>([]);
  const [names, setNames] = useState<Record<number, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    try {
      const [c, p] = await Promise.all([apiClient.clusters(), apiClient.persons()]);
      setClusters(c);
      setPersons(p);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function nameCluster(id: number) {
    const name = names[id]?.trim();
    if (!name) return;
    await apiClient.nameCluster(id, name);
    await load();
  }

  async function ignoreCluster(id: number) {
    await apiClient.ignoreCluster(id);
    await load();
  }

  async function mergeCluster(id: number, personId: number) {
    await apiClient.mergeCluster(id, personId);
    await load();
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-semibold">Unknown Faces</h2>
        <p className="text-sm text-zinc-400">Name a person once — future appearances auto-tag</p>
      </div>

      {error && <Card className="border-red-800 text-red-300">{error}</Card>}
      {loading && <p className="text-zinc-400">Loading...</p>}

      {!loading && clusters.length === 0 && (
        <Card>
          <p className="text-zinc-400">No unknown faces in the review queue. Index some images to get started.</p>
        </Card>
      )}

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {clusters.map((c) => (
          <Card key={c.id} className="space-y-3">
            <div className="flex gap-3">
              <FaceThumb faceId={c.representative_face_id} className="h-20 w-20" />
              <div>
                <p className="font-medium">Cluster #{c.id}</p>
                <p className="text-sm text-zinc-400">{c.member_count} face(s)</p>
                {c.representative_confidence != null && (
                  <p className="text-xs text-zinc-500">{(c.representative_confidence * 100).toFixed(0)}% confidence</p>
                )}
              </div>
            </div>
            <div className="text-xs text-zinc-500">
              {c.appears_in.map((m) => m.path).join(", ")}
            </div>
      <div className="flex flex-col gap-2 sm:flex-row">
        <Input
          className="flex-1"
          placeholder="Enter name..."
          value={names[c.id] ?? ""}
          onChange={(e) => setNames((n) => ({ ...n, [c.id]: e.target.value }))}
        />
        <Button className="shrink-0" onClick={() => nameCluster(c.id)}>Name</Button>
      </div>
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" onClick={() => ignoreCluster(c.id)}>
                Ignore
              </Button>
              {persons.length > 0 && (
                <select
                  className="rounded-md border border-border bg-background px-2 py-1 text-sm"
                  onChange={(e) => e.target.value && mergeCluster(c.id, Number(e.target.value))}
                  defaultValue=""
                >
                  <option value="" disabled>
                    Merge into...
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
        ))}
      </div>
    </div>
  );
}
