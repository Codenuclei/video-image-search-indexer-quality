"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { Pencil } from "lucide-react";
import { apiClient, type Person } from "@/lib/api";
import { Button, Card, FaceThumb, Input } from "@/components/ui";

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
  const id = Number(params.id);
  const [person, setPerson] = useState<Person | null>(null);
  const [media, setMedia] = useState<PersonMedia[]>([]);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    apiClient.person(id).then((p) => {
      setPerson(p);
      setName(p.name);
    });
    apiClient.personMedia(id).then(setMedia);
  }, [id]);

  async function saveName() {
    if (!person) return;
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Name cannot be empty");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const updated = await apiClient.renamePerson(person.id, trimmed);
      setPerson(updated);
      setName(updated.name);
      setEditing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Rename failed");
    } finally {
      setSaving(false);
    }
  }

  function cancelEdit() {
    if (person) setName(person.name);
    setError(null);
    setEditing(false);
  }

  if (!person) return <p className="text-zinc-400">Loading...</p>;

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
                  if (e.key === "Enter") saveName();
                  if (e.key === "Escape") cancelEdit();
                }}
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
                <p className="text-sm text-zinc-400">{person.occurrence_count} appearances across Drive</p>
              </div>
              <button
                type="button"
                onClick={() => setEditing(true)}
                title="Edit name"
                className="mt-1 shrink-0 rounded-md p-2 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              >
                <Pencil size={16} />
              </button>
            </div>
          )}
        </div>
      </div>

      <Card>
        <h3 className="mb-3 font-medium">Appears in</h3>
        <ul className="space-y-2">
          {media.map((m) => (
            <li key={m.media_id} className="rounded-md bg-muted/50 px-3 py-2 text-sm">
              <span className="font-medium">{m.name}</span>
              <span className="ml-2 text-zinc-500">{m.path}</span>
              <span className="ml-2 text-xs text-zinc-600">({m.media_type})</span>
              {m.media_type === "video" && m.frame_timestamp != null && (
                <span className="ml-2 text-xs text-violet-400">@ {formatTimestamp(m.frame_timestamp)}</span>
              )}
            </li>
          ))}
        </ul>
        {media.length === 0 && <p className="text-sm text-zinc-500">No media linked yet.</p>}
      </Card>
    </div>
  );
}
