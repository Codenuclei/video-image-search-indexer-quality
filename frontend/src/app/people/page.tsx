"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Pencil, Trash2 } from "lucide-react";
import { apiClient, type Person, type PersonRole } from "@/lib/api";
import { Button, Card, ConfirmDialog, FaceThumb, Input } from "@/components/ui";
import { RoleSelector } from "@/components/role-selector";

function PersonCard({
  person,
  onRenamed,
  onDeleted,
}: {
  person: Person;
  onRenamed: (updated: Person) => void;
  onDeleted: (id: number) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(person.name);
  const [saving, setSaving] = useState(false);
  const [roleSaving, setRoleSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const savingRef = useRef(false);

  useEffect(() => {
    if (!editing) setName(person.name);
  }, [person.name, editing]);

  async function save() {
    if (savingRef.current || saving) return;
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Name cannot be empty");
      return;
    }
    savingRef.current = true;
    setSaving(true);
    setError(null);
    try {
      const updated = await apiClient.renamePerson(person.id, trimmed);
      onRenamed(updated);
      setEditing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Rename failed");
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  async function saveRole(nextRole: PersonRole) {
    if (roleSaving || nextRole === person.role) return;
    setRoleSaving(true);
    setError(null);
    try {
      const updated = await apiClient.updatePerson(person.id, { role: nextRole });
      onRenamed(updated);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not update role");
    } finally {
      setRoleSaving(false);
    }
  }

  async function remove() {
    setDeleting(true);
    setError(null);
    try {
      await apiClient.deletePerson(person.id);
      onDeleted(person.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Delete failed");
      setDeleting(false);
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
                  if (e.key === "Enter") {
                    e.preventDefault();
                    save();
                  }
                  if (e.key === "Escape") cancel();
                }}
                disabled={saving}
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
                <div className="flex shrink-0 gap-1">
                  <button
                    type="button"
                    onClick={() => setEditing(true)}
                    title="Edit name"
                    className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                  >
                    <Pencil size={14} />
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmDelete(true)}
                    disabled={deleting}
                    title="Delete name"
                    className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive disabled:opacity-50"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
              <p className="text-sm text-muted-foreground">{person.occurrence_count} appearances</p>
              <div className="mt-3 space-y-2">
                <p className="text-xs text-muted-foreground">Role tag</p>
                <RoleSelector role={person.role ?? null} disabled={roleSaving} onChange={saveRole} />
              </div>
              {error && <p className="text-xs text-destructive">{error}</p>}
            </>
          )}
        </div>
      </div>
      <ConfirmDialog
        open={confirmDelete}
        title={`Delete "${person.name}"?`}
        message="Faces will be unlinked and may return to the review queue."
        confirmLabel={deleting ? "Deleting…" : "Delete"}
        onConfirm={() => {
          setConfirmDelete(false);
          remove();
        }}
        onCancel={() => setConfirmDelete(false)}
      />
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
        <p className="text-sm text-muted-foreground">
          Everyone recognized across your Drive. Mark people as Student or Non-student to improve
          searches like &quot;teacher with students&quot;.
        </p>
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
            onDeleted={(id) => setPersons((prev) => prev.filter((item) => item.id !== id))}
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
