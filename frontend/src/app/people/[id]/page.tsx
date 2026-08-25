"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, usePathname, useRouter } from "next/navigation";
import { ArrowLeft, FileVideo, Pencil } from "lucide-react";
import {
  apiClient,
  driveFileThumbnailUrl,
  driveGoogleViewUrl,
  formatApiError,
  type Person,
  type PersonRole,
} from "@/lib/api";
import { Button, Card, ConfirmDialog, FaceThumb, Input, LoadingLabel } from "@/components/ui";
import { RoleSelector } from "@/components/role-selector";
import { AnimatedTrash } from "@/components/animated-trash";
import { useRegisterTestShellChrome } from "@/lib/test-shell-chrome";

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
  const pathname = usePathname();
  const inTestShell = pathname.startsWith("/test");
  const peopleListHref = inTestShell ? "/test/people" : "/people";
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
      setError(formatApiError(e, "Rename failed"));
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
      setError(formatApiError(e, "Could not update role"));
    } finally {
      setRoleSaving(false);
    }
  }

  async function deleteName() {
    if (!person) return;
    setDeleting(true);
    setError(null);
    router.push(peopleListHref);
    try {
      await apiClient.deletePerson(person.id);
    } catch (e) {
      setError(formatApiError(e, "Delete failed"));
      setDeleting(false);
    }
  }

  function cancelEdit() {
    if (person) setName(person.name);
    setError(null);
    setEditing(false);
  }

  const mediaBreakdown = useMemo(() => {
    let images = 0;
    let videos = 0;
    for (const m of media) {
      const t = (m.media_type || "").toLowerCase();
      if (t === "video") videos += 1;
      else if (t === "image") images += 1;
    }
    return { images, videos };
  }, [media]);

  const mediaSummary =
    media.length > 0
      ? `${media.length} file${media.length === 1 ? "" : "s"} · ${mediaBreakdown.images} image${
          mediaBreakdown.images === 1 ? "" : "s"
        } · ${mediaBreakdown.videos} video${mediaBreakdown.videos === 1 ? "" : "s"}`
      : "No files yet";

  useRegisterTestShellChrome(
    inTestShell && person ? (
      <div className="flex min-w-0 flex-1 items-center gap-2 sm:gap-3">
        <button
          type="button"
          onClick={() => router.push(peopleListHref)}
          title="Back to People Directory"
          className="flex h-8 shrink-0 items-center gap-1 rounded-full border border-border bg-card px-2.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <ArrowLeft size={14} aria-hidden />
          <span className="hidden sm:inline">Back</span>
        </button>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-base font-semibold tracking-tight sm:text-lg">{person.name}</h1>
          <p className="truncate text-[11px] tabular-nums text-muted-foreground sm:text-xs">{mediaSummary}</p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            onClick={() => setEditing(true)}
            title="Edit name"
            className="flex h-8 w-8 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <Pencil size={15} />
          </button>
          <Button
            variant="secondary"
            onClick={() => setConfirmDelete(true)}
            disabled={deleting}
            className="group/trash h-8 px-2.5 text-xs text-destructive hover:border-destructive/40 hover:text-destructive"
          >
            <span className="inline-flex items-center gap-1.5">
              <AnimatedTrash size={14} animating={deleting} />
              <span className="hidden sm:inline">{deleting ? "Deleting…" : "Delete"}</span>
            </span>
          </Button>
        </div>
      </div>
    ) : null,
    [inTestShell, person, mediaSummary, deleting, peopleListHref]
  );

  if (!person) {
    return (
      <p className="text-muted-foreground">
        <LoadingLabel size={16}>Loading…</LoadingLabel>
      </p>
    );
  }

  return (
    <div className="space-y-6">
      {!inTestShell && (
        <button
          type="button"
          onClick={() => router.back()}
          className="inline-flex items-center gap-1.5 rounded-full border border-border bg-card px-3 py-1.5 text-xs font-medium text-muted-foreground shadow-sm transition-colors hover:bg-muted hover:text-foreground"
        >
          <ArrowLeft size={14} aria-hidden />
          Back
        </button>
      )}

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
                  {saving ? <LoadingLabel>Saving…</LoadingLabel> : "Save"}
                </Button>
                <Button variant="secondary" onClick={cancelEdit} disabled={saving}>
                  Cancel
                </Button>
              </div>
            </div>
          ) : inTestShell ? (
            <div className="space-y-2">
              <p className="text-xs text-muted-foreground">Role tag (used for student / teacher search)</p>
              <RoleSelector role={person.role ?? null} disabled={roleSaving} onChange={saveRole} />
              {error && <p className="text-sm text-destructive">{error}</p>}
            </div>
          ) : (
            <div className="flex items-start gap-2">
              <div>
                <h2 className="text-xl font-semibold sm:text-2xl">{person.name}</h2>
                <p className="text-sm text-muted-foreground">{mediaSummary}</p>
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
                  className="group/trash text-destructive hover:border-destructive/40 hover:text-destructive"
                >
                  <span className="inline-flex items-center gap-1.5">
                    <AnimatedTrash size={14} animating={deleting} />
                    {deleting ? "Deleting…" : "Delete name"}
                  </span>
                </Button>
              </div>
            </div>
          )}
          {error && !editing && !inTestShell && <p className="mt-2 text-sm text-destructive">{error}</p>}
        </div>
      </div>

      <Card>
        <div className="mb-3">
          <h3 className="font-medium">Appears in</h3>
        </div>
        {media.length === 0 ? (
          <p className="text-sm text-muted-foreground">No media linked yet.</p>
        ) : (
          <ul className="grid grid-cols-4 gap-2 sm:grid-cols-6 md:grid-cols-8 lg:grid-cols-10">
            {media.map((m) => {
              const isVideo = (m.media_type || "").toLowerCase() === "video";
              return (
                <li key={m.media_id}>
                  <a
                    href={driveGoogleViewUrl(m.drive_file_id)}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={m.name}
                    className="relative block aspect-square overflow-hidden rounded-md border border-border bg-muted/40 transition-opacity hover:opacity-90"
                  >
                    {isVideo ? (
                      <span className="flex h-full w-full flex-col items-center justify-center gap-0.5 text-muted-foreground">
                        <FileVideo size={18} aria-hidden />
                        {m.frame_timestamp != null && (
                          <span className="text-[9px] tabular-nums">{formatTimestamp(m.frame_timestamp)}</span>
                        )}
                      </span>
                    ) : (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={driveFileThumbnailUrl(m.drive_file_id)}
                        alt={m.name}
                        loading="lazy"
                        decoding="async"
                        className="h-full w-full object-cover"
                      />
                    )}
                  </a>
                </li>
              );
            })}
          </ul>
        )}
      </Card>

      <ConfirmDialog
        open={confirmDelete}
        title={`Delete "${person.name}"?`}
        message="Faces will be unlinked and may return to the review queue."
        confirmLabel={deleting ? <LoadingLabel>Deleting…</LoadingLabel> : "Delete"}
        onConfirm={() => {
          setConfirmDelete(false);
          deleteName();
        }}
        onCancel={() => setConfirmDelete(false)}
      />
    </div>
  );
}
