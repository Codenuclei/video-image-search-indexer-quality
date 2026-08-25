"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Images, Pencil } from "lucide-react";
import { apiClient, formatApiError, type Person, type PersonRole } from "@/lib/api";
import { Button, Card, ConfirmDialog, FaceThumb, Input, LoadingLabel } from "@/components/ui";
import { RoleSelector } from "@/components/role-selector";
import { AnimatedTrash } from "@/components/animated-trash";
import { cn } from "@/lib/utils";
import { useCachedResource } from "@/lib/use-cached-resource";
import { writeCache } from "@/lib/data-cache";
import { personsRevision } from "@/lib/fingerprints";

function PersonCard({
  person,
  onRenamed,
  onDeleted,
  onDeleteFailed,
  personHref,
}: {
  person: Person;
  onRenamed: (updated: Person) => void;
  onDeleted: (id: number) => void;
  onDeleteFailed: (person: Person) => void;
  /** Base path for person detail links (e.g. /people or /test/people). */
  personHref?: (id: number) => string;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(person.name);
  const [saving, setSaving] = useState(false);
  const [roleSaving, setRoleSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [vanishing, setVanishing] = useState(false);
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
    const optimistic = { ...person, name: trimmed };
    onRenamed(optimistic);
    setEditing(false);
    try {
      const updated = await apiClient.renamePerson(person.id, trimmed);
      onRenamed(updated);
    } catch (e) {
      onRenamed(person);
      setEditing(true);
      setName(person.name);
      setError(formatApiError(e, "Rename failed"));
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
      setError(formatApiError(e, "Could not update role"));
    } finally {
      setRoleSaving(false);
    }
  }

  async function remove() {
    setDeleting(true);
    setError(null);
    // Let the dustbin + card exit animation play before the card unmounts.
    setVanishing(true);
    await new Promise((resolve) => setTimeout(resolve, 420));
    onDeleted(person.id);
    try {
      await apiClient.deletePerson(person.id);
    } catch (e) {
      onDeleteFailed(person);
      setVanishing(false);
      setError(formatApiError(e, "Delete failed"));
      setDeleting(false);
    }
  }

  function cancel() {
    setName(person.name);
    setError(null);
    setEditing(false);
  }

  const detailHref = personHref?.(person.id) ?? `/people/${person.id}`;

  return (
    <Card
      className={cn(
        "group relative flex min-w-0 flex-col gap-3 overflow-hidden p-3 transition-all duration-200 sm:gap-4 sm:p-4",
        "hover:-translate-y-0.5 hover:border-border hover:shadow-[0_12px_40px_-18px_rgba(0,0,0,0.35)]",
        vanishing && "card-vanish"
      )}
    >
      {editing ? (
        <div className="space-y-3">
          <div className="flex items-start gap-3">
            <Link href={detailHref} className="shrink-0">
              <FaceThumb
                faceId={person.representative_face_id}
                className="h-14 w-14 rounded-xl object-cover ring-1 ring-border"
              />
            </Link>
            <div className="min-w-0 flex-1 space-y-2">
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
              <div className="flex flex-wrap gap-2">
                <Button onClick={save} disabled={saving}>
                  {saving ? <LoadingLabel>Saving…</LoadingLabel> : "Save"}
                </Button>
                <Button variant="secondary" onClick={cancel} disabled={saving}>
                  Cancel
                </Button>
              </div>
            </div>
          </div>
        </div>
      ) : (
        <>
          <div className="flex items-start gap-3">
            <Link href={detailHref} className="shrink-0">
              <FaceThumb
                faceId={person.representative_face_id}
                className="h-12 w-12 rounded-xl object-cover ring-1 ring-border transition-transform duration-200 group-hover:scale-[1.02] sm:h-14 sm:w-14"
              />
            </Link>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-start justify-between gap-x-2 gap-y-1">
                <div className="min-w-0 flex-1 basis-[calc(100%-3rem)] sm:basis-auto">
                  <Link
                    href={detailHref}
                    className="block break-words text-sm font-semibold leading-snug tracking-tight text-foreground hover:underline sm:text-[15px]"
                  >
                    {person.name}
                  </Link>
                  <p className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted-foreground">
                    <Images size={12} aria-hidden className="shrink-0 opacity-70" />
                    <span>
                      {person.file_count ?? person.occurrence_count} file
                      {(person.file_count ?? person.occurrence_count) === 1 ? "" : "s"}
                    </span>
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-0.5 rounded-lg border border-transparent bg-transparent p-0.5 transition-colors group-hover:border-border/60 group-hover:bg-muted/40">
                  <button
                    type="button"
                    onClick={() => setEditing(true)}
                    title="Edit name"
                    className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-background hover:text-foreground hover:shadow-sm"
                  >
                    <Pencil size={13} />
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmDelete(true)}
                    disabled={deleting}
                    title="Delete name"
                    className="group/trash rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive disabled:opacity-50"
                  >
                    <AnimatedTrash size={13} animating={deleting} />
                  </button>
                </div>
              </div>
            </div>
          </div>

          <div className="space-y-1.5 border-t border-border/60 pt-3">
            <p className="text-[10px] font-medium uppercase tracking-[0.14em] text-muted-foreground">Role</p>
            <RoleSelector
              role={person.role ?? null}
              disabled={roleSaving}
              onChange={saveRole}
              variant="card"
              className="w-full"
            />
          </div>
          {error && <p className="text-xs text-destructive">{error}</p>}
        </>
      )}
      <ConfirmDialog
        open={confirmDelete}
        title={`Delete "${person.name}"?`}
        message="Faces will be unlinked and may return to the review queue."
        confirmLabel={deleting ? <LoadingLabel>Deleting…</LoadingLabel> : "Delete"}
        onConfirm={() => {
          setConfirmDelete(false);
          remove();
        }}
        onCancel={() => setConfirmDelete(false)}
      />
    </Card>
  );
}

export function PeoplePage({
  embedded = false,
  personHref,
}: {
  embedded?: boolean;
  /** Base path for person detail links (e.g. /people or /test/people). */
  personHref?: (id: number) => string;
} = {}) {
  const {
    data: persons,
    loading,
    error: cacheError,
    refresh,
  } = useCachedResource<Person[]>({
    key: "persons",
    fetcher: () => apiClient.persons(),
    getRevision: async () => {
      const meta = await apiClient.personsRevision();
      return meta.revision;
    },
    revisionFromData: (items) => personsRevision(items),
    pollMs: 60000,
  });

  const list = persons ?? [];
  const error = cacheError;

  return (
    <div className="space-y-6">
      {!embedded && (
      <div>
        <h2 className="text-xl font-semibold sm:text-2xl">People</h2>
        <p className="text-sm text-muted-foreground">
          Everyone recognized across your Drive. Mark people as Student or Non-student to improve
          searches like &quot;teacher with students&quot;.
        </p>
      </div>
      )}

      {loading && (
        <p className="text-sm text-muted-foreground">
          <LoadingLabel size={16}>Loading people…</LoadingLabel>
        </p>
      )}

      <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,17.5rem),1fr))] gap-4">
        {list.map((p) => (
          <PersonCard
            key={p.id}
            person={p}
            onRenamed={(updated) => {
              const next = list.map((item) => (item.id === updated.id ? updated : item));
              writeCache("persons", next, personsRevision(next), true);
              void refresh(true);
            }}
            onDeleted={(id) => {
              const next = list.filter((item) => item.id !== id);
              writeCache("persons", next, personsRevision(next), true);
              void refresh(true);
            }}
            onDeleteFailed={(restored) => {
              const next = list.some((item) => item.id === restored.id)
                ? list
                : [...list, restored].sort((a, b) => a.name.localeCompare(b.name));
              writeCache("persons", next, personsRevision(next), true);
            }}
            personHref={personHref}
          />
        ))}
      </div>

      {!loading && list.length === 0 && !error && (
        <Card>
          <p className="text-muted-foreground">No people named yet.</p>
        </Card>
      )}
    </div>
  );
}
