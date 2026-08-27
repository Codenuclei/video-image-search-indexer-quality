"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, usePathname, useRouter } from "next/navigation";
import { ArrowLeft, Check, FileVideo, Pencil, X } from "lucide-react";
import {
  apiClient,
  driveFileThumbnailUrl,
  driveGoogleViewUrl,
  formatApiError,
  type Person,
  type PersonClusterSuggestion,
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
  const [suggestions, setSuggestions] = useState<PersonClusterSuggestion[]>([]);
  const [suggestionTotal, setSuggestionTotal] = useState(0);
  const [suggestionsLoading, setSuggestionsLoading] = useState(true);
  const [suggestionsLoadingMore, setSuggestionsLoadingMore] = useState(false);
  const [suggestionActionId, setSuggestionActionId] = useState<number | null>(null);
  const [suggestionError, setSuggestionError] = useState<string | null>(null);
  const savingRef = useRef(false);

  useEffect(() => {
    if (!id) return;
    apiClient.person(id).then((p) => {
      setPerson(p);
      setName(p.name);
    });
    apiClient.personMedia(id).then(setMedia);
    setSuggestionsLoading(true);
    apiClient
      .personClusterSuggestions(id, { limit: 12, offset: 0 })
      .then((result) => {
        setSuggestions(result.items);
        setSuggestionTotal(result.total);
        setSuggestionError(null);
      })
      .catch((e) => setSuggestionError(formatApiError(e, "Could not load potential matches")))
      .finally(() => setSuggestionsLoading(false));
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

  async function decideSuggestion(clusterId: number, decision: "accept" | "reject") {
    if (!person || suggestionActionId != null) return;
    const previous = suggestions;
    setSuggestionActionId(clusterId);
    setSuggestionError(null);
    setSuggestions((items) => items.filter((item) => item.cluster_id !== clusterId));
    setSuggestionTotal((total) => Math.max(0, total - 1));
    try {
      if (decision === "accept") {
        const updated = await apiClient.acceptPersonClusterSuggestion(person.id, clusterId);
        setPerson(updated);
        setName(updated.name);
        setMedia(await apiClient.personMedia(person.id));
      } else {
        await apiClient.rejectPersonClusterSuggestion(person.id, clusterId);
      }
    } catch (e) {
      setSuggestions(previous);
      setSuggestionTotal((total) => total + 1);
      setSuggestionError(formatApiError(e, "Could not save this decision"));
    } finally {
      setSuggestionActionId(null);
    }
  }

  async function loadMoreSuggestions() {
    if (!person || suggestionsLoadingMore || suggestions.length >= suggestionTotal) return;
    setSuggestionsLoadingMore(true);
    setSuggestionError(null);
    try {
      const result = await apiClient.personClusterSuggestions(person.id, {
        limit: 12,
        offset: suggestions.length,
      });
      setSuggestions((items) => [...items, ...result.items]);
      setSuggestionTotal(result.total);
    } catch (e) {
      setSuggestionError(formatApiError(e, "Could not load more potential matches"));
    } finally {
      setSuggestionsLoadingMore(false);
    }
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
    <div className="min-w-0 max-w-full space-y-6 overflow-x-hidden">
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

      <div className="flex min-w-0 flex-col items-start gap-4 sm:flex-row sm:items-center">
        <FaceThumb faceId={person.representative_face_id} className="h-24 w-24 shrink-0" />
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
            <div className="w-fit max-w-full space-y-1.5">
              <p className="text-[11px] text-muted-foreground">Role tag</p>
              <RoleSelector role={person.role ?? null} disabled={roleSaving} onChange={saveRole} />
              {error && <p className="text-sm text-destructive">{error}</p>}
            </div>
          ) : (
            <div className="flex min-w-0 items-start gap-2">
              <div className="min-w-0 flex-1">
                <h2 className="truncate text-xl font-semibold sm:text-2xl">{person.name}</h2>
                <p className="text-sm text-muted-foreground">{mediaSummary}</p>
                <div className="mt-3 w-fit max-w-full space-y-1.5">
                  <p className="text-[11px] text-muted-foreground">Role tag</p>
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

      <Card className="min-w-0 overflow-hidden p-0">
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            <h3 className="font-medium">Potential matches</h3>
            <p className="text-xs text-muted-foreground">
              Unknown face clusters at least 50% similar to {person.name}
            </p>
          </div>
          {suggestionTotal > 0 && (
            <span className="shrink-0 rounded-full bg-primary/10 px-2.5 py-1 text-xs font-semibold tabular-nums text-primary">
              {suggestionTotal} suggested
            </span>
          )}
        </div>
        {suggestionsLoading ? (
          <p className="px-4 py-6 text-sm text-muted-foreground">
            <LoadingLabel>Finding matches…</LoadingLabel>
          </p>
        ) : suggestions.length === 0 ? (
          <p className="px-4 py-6 text-sm text-muted-foreground">
            No unreviewed clusters currently match this person above 50%.
          </p>
        ) : (
          <ul className="divide-y divide-border">
            {suggestions.map((suggestion) => {
              const percent = Math.round(suggestion.similarity * 100);
              const busy = suggestionActionId === suggestion.cluster_id;
              return (
                <li
                  key={suggestion.cluster_id}
                  className="flex min-w-0 flex-col gap-3 px-4 py-3 lg:flex-row lg:items-center"
                >
                  <div className="flex min-w-0 flex-1 items-center gap-3">
                    <FaceThumb
                      faceId={suggestion.representative_face_id}
                      className="h-14 w-14 shrink-0"
                    />
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span
                          className={`rounded-full px-2 py-0.5 text-xs font-semibold tabular-nums ${
                            percent >= 60
                              ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
                              : "bg-amber-500/15 text-amber-700 dark:text-amber-300"
                          }`}
                        >
                          {percent}% match
                        </span>
                        <span className="text-xs text-muted-foreground">
                          {suggestion.member_count} face{suggestion.member_count === 1 ? "" : "s"} ·{" "}
                          {suggestion.file_count} file{suggestion.file_count === 1 ? "" : "s"}
                        </span>
                      </div>
                      {suggestion.sample_files.length > 0 && (
                        <p
                          className="mt-1 truncate text-xs text-muted-foreground"
                          title={suggestion.sample_files.map((file) => file.name).join(", ")}
                        >
                          {suggestion.sample_files.map((file) => file.name).join(" · ")}
                        </p>
                      )}
                    </div>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    <Button
                      onClick={() => decideSuggestion(suggestion.cluster_id, "accept")}
                      disabled={suggestionActionId != null}
                      className="min-w-0"
                    >
                      <Check size={15} aria-hidden />
                      {busy ? "Saving…" : `Add to ${person.name}`}
                    </Button>
                    <Button
                      variant="secondary"
                      onClick={() => decideSuggestion(suggestion.cluster_id, "reject")}
                      disabled={suggestionActionId != null}
                      title={`This cluster is not ${person.name}`}
                    >
                      <X size={15} aria-hidden />
                      Not {person.name}
                    </Button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
        {(suggestionError || suggestions.length < suggestionTotal) && (
          <div className="flex items-center justify-between gap-3 border-t border-border px-4 py-3">
            {suggestionError ? (
              <p className="text-sm text-destructive">{suggestionError}</p>
            ) : (
              <span />
            )}
            {suggestions.length < suggestionTotal && (
              <Button
                variant="secondary"
                onClick={loadMoreSuggestions}
                disabled={suggestionsLoadingMore}
              >
                {suggestionsLoadingMore ? <LoadingLabel>Loading…</LoadingLabel> : "Load more"}
              </Button>
            )}
          </div>
        )}
      </Card>

      <Card className="flex min-h-0 min-w-0 max-h-[min(28rem,calc(100dvh-14rem))] flex-col overflow-hidden p-0">
        <div className="flex shrink-0 items-baseline justify-between gap-2 border-b border-border px-4 py-3">
          <h3 className="font-medium">Appears in</h3>
          {media.length > 0 && (
            <p className="text-xs tabular-nums text-muted-foreground">
              {mediaBreakdown.images} img · {mediaBreakdown.videos} vid
            </p>
          )}
        </div>
        {media.length === 0 ? (
          <p className="px-4 py-6 text-sm text-muted-foreground">No media linked yet.</p>
        ) : (
          <div className="scrollbar-hidden min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-4 py-3">
            <ul className="grid grid-cols-[repeat(auto-fill,minmax(4.5rem,1fr))] gap-2">
              {media.map((m) => {
                const isVideo = (m.media_type || "").toLowerCase() === "video";
                return (
                  <li key={m.media_id} className="min-w-0">
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
                          className="h-full w-full max-w-full object-cover"
                        />
                      )}
                    </a>
                  </li>
                );
              })}
            </ul>
          </div>
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
