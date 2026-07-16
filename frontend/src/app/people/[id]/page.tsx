"use client";

import { useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { Pencil } from "lucide-react";
import { apiClient, type Person, type PersonRole } from "@/lib/api";
import { Button, Card, ConfirmDialog, FaceThumb, Input } from "@/components/ui";
import { RoleSelector } from "@/components/role-selector";

type PersonMedia = {
  media_id: number;
  drive_file_id: string;
  name: string;
  path: string;
  media_type: string;
  frame_timestamp?: number | null;
};

function formatTimestamp(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export default function PersonDetailPage() {
  const params = useParams();
  const router = useRouter();
  const id = Number(params.id);
  const [person, setPerson] = useState<Person | null>(null);
  const [media, setMedia] = useState<PersonMedia[]>([]);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [roleSaving, setRoleSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const savingRef = useRef(false);

  useEffect(() => {
    if (!id) return;
    apiClient.person(id).then((p) => {
      setPerson(p);
      setName(p.name);
    });
    apiClient.personMedia(id).then(setMedia);
  }, [id]);

  async function saveName() {
    if (!person || savingRef.current || saving) return;
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Name cannot be empty");
      return;
    }
    savingRef.current = true;
    setSaving(true);
    setError(null);
    const previous = person;
    setPerson({ ...person, name: trimmed });
    setName(trimmed);
    setEditing(false);
    try {
      const updated = await apiClient.renamePerson(person.id, trimmed);
      setPerson(updated);
      setName(updated.name);
    } catch (e) {
      setPerson(previous);
      setName(previous.name);
      setEditing(true);
      setError(e instanceof Error ? e.message : "Rename failed");
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  async function saveRole(nextRole: PersonRole) {
    if (!person || roleSaving || nextRole === person.role) return;
    setRoleSaving(true);
    setError(null);
    try {
      const updated = await apiClient.updatePerson(person.id, { role: nextRole });
      setPerson(updated);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not update role");
    } finally {
      setRoleSaving(false);
    }
  }

  async function deleteName() {
    if (!person) return;
    setDeleting(true);
    setError(null);
    router.push("/people");
    try {
      await apiClient.deletePerson(person.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Delete failed");
      setDeleting(false);
    }
  }

  function cancelEdit() {
    if (person) setName(person.name);
    setError(null);
    setEditing(false);
  }

  if (!person) return <p className="text-muted-foreground">Loading...</p>;

  return (
    <div className="space-y-6">
      <div className="flex flex-col items-start gap-4 sm:flex-row sm:items-center">
        <FaceThumb faceId={person.representative_face_id} className="h-24 w-24" />
        <div className="min-w-0 flex-1">
          {editing ? (
            <div className="max-w-md space-y-2">
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    saveName();
                  }
                  if (e.key === "Escape") cancelEdit();
                }}
                disabled={saving}
                autoFocus
              />
              {error && <p className="text-sm text-destructive">{error}</p>}
              <div className="flex gap-2">
                <Button onClick={saveName} disabled={saving}>
                  {saving ? "Saving…" : "Save"}
                </Button>
                <Button variant="secondary" onClick={cancelEdit} disabled={saving}>
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex items-start gap-2">
              <div>
                <h2 className="text-xl font-semibold sm:text-2xl">{person.name}</h2>
                <p className="text-sm text-muted-foreground">{person.occurrence_count} appearances across Drive</p>
                <div className="mt-3 space-y-2">
                  <p className="text-xs text-muted-foreground">Role tag (used for student / teacher search)</p>
                  <RoleSelector role={person.role ?? null} disabled={roleSaving} onChange={saveRole} />
                </div>
              </div>
              <div className="mt-1 flex shrink-0 gap-1">
                <button
                  type="button"
                  onClick={() => setEditing(true)}
                  title="Edit name"
                  className="rounded-md p-2 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                >
                  <Pencil size={16} />
                </button>
                <Button
                  variant="secondary"
                  onClick={() => setConfirmDelete(true)}
                  disabled={deleting}
                  className="text-destructive hover:text-destructive"
                >
                  {deleting ? "Deleting…" : "Delete name"}
                </Button>
              </div>
            </div>
          )}
          {error && !editing && <p className="mt-2 text-sm text-destructive">{error}</p>}
        </div>
      </div>

      <Card>
        <h3 className="mb-3 font-medium">Appears in</h3>
        <ul className="space-y-2">
          {media.map((m) => (
            <li key={m.media_id} className="rounded-md bg-muted/50 px-3 py-2 text-sm">
              <span className="font-medium">{m.name}</span>
              <span className="ml-2 text-muted-foreground">{m.path}</span>
              <span className="ml-2 text-xs text-muted-foreground">({m.media_type})</span>
              {m.media_type === "video" && m.frame_timestamp != null && (
                <span className="ml-2 text-xs text-violet-400">@ {formatTimestamp(m.frame_timestamp)}</span>
              )}
            </li>
          ))}
        </ul>
        {media.length === 0 && <p className="text-sm text-muted-foreground">No media linked yet.</p>}
      </Card>

      {person && (
        <ConfirmDialog
          open={confirmDelete}
          title={`Delete "${person.name}"?`}
          message="Faces will be unlinked and may return to the review queue."
          confirmLabel={deleting ? "Deleting…" : "Delete"}
          onConfirm={() => {
            setConfirmDelete(false);
            deleteName();
          }}
          onCancel={() => setConfirmDelete(false)}
        />
      )}
    </div>
  );
}
