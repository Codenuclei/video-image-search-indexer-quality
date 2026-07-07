"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Pencil } from "lucide-react";
import { apiClient, type Person } from "@/lib/api";
import { Button, Card, FaceThumb, Input } from "@/components/ui";

function PersonCard({
  person,
  onRenamed,
}: {
  person: Person;
  onRenamed: (updated: Person) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(person.name);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!editing) setName(person.name);
  }, [person.name, editing]);

  async function save() {
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Name cannot be empty");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const updated = await apiClient.renamePerson(person.id, trimmed);
      onRenamed(updated);
      setEditing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Rename failed");
    } finally {
      setSaving(false);
    }
  }

  function cancel() {
    setName(person.name);
    setError(null);
    setEditing(false);
  }

  return (
    <Card className="flex flex-col gap-3">
      <div className="flex items-center gap-3">
        <Link href={`/people/${person.id}`} className="shrink-0">
          <FaceThumb faceId={person.representative_face_id} className="h-14 w-14 rounded-lg" />
        </Link>
        <div className="min-w-0 flex-1">
          {editing ? (
            <div className="space-y-2">
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") save();
                  if (e.key === "Escape") cancel();
                }}
                autoFocus
              />
              {error && <p className="text-xs text-destructive">{error}</p>}
              <div className="flex gap-2">
                <Button onClick={save} disabled={saving}>
                  {saving ? "Saving…" : "Save"}
                </Button>
                <Button variant="secondary" onClick={cancel} disabled={saving}>
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <>
              <div className="flex items-start justify-between gap-2">
                <Link href={`/people/${person.id}`} className="min-w-0 hover:underline">
                  <p className="truncate font-medium">{person.name}</p>
                </Link>
                <button
                  type="button"
                  onClick={() => setEditing(true)}
                  title="Edit name"
                  className="shrink-0 rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                >
                  <Pencil size={14} />
                </button>
              </div>
              <p className="text-sm text-muted-foreground">{person.occurrence_count} appearances</p>
            </>
          )}
        </div>
      </div>
    </Card>
  );
}

export default function PeoplePage() {
  const [persons, setPersons] = useState<Person[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiClient.persons().then(setPersons).catch((e) => setError(e.message));
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold sm:text-2xl">People</h2>
        <p className="text-sm text-muted-foreground">Everyone recognized across your Drive</p>
      </div>

      {error && <Card className="border-destructive text-destructive">{error}</Card>}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {persons.map((p) => (
          <PersonCard
            key={p.id}
            person={p}
            onRenamed={(updated) =>
              setPersons((prev) => prev.map((item) => (item.id === updated.id ? updated : item)))
            }
          />
        ))}
      </div>

      {persons.length === 0 && !error && (
        <Card>
          <p className="text-muted-foreground">No people named yet.</p>
        </Card>
      )}
    </div>
  );
}
