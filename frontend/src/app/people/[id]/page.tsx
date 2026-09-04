"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, usePathname, useRouter } from "next/navigation";
import { ArrowLeft, Check, ChevronDown, Pencil, X } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  apiClient,
  driveGoogleViewUrl,
  formatApiError,
  type Person,
  type PersonClusterSuggestion,
  type PersonRole,
} from "@/lib/api";
import { Button, Card, ConfirmDialog, DriveMediaThumb, FaceThumb, Input, LoadingLabel } from "@/components/ui";
import { RoleSelector } from "@/components/role-selector";
import { AnimatedTrash } from "@/components/animated-trash";
import { useRegisterTestShellChrome } from "@/lib/test-shell-chrome";

type ClusterFile = {
  media_id: number;
  drive_file_id: string;
  name: string;
  path: string;
  media_type: string;
  frame_timestamp?: number | null;
};

type PersonMedia = ClusterFile;

type ClusterPreview = {
  files: ClusterFile[];
  faceIds: number[];
};

function formatTimestamp(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function isVideoFile(file: ClusterFile): boolean {
  return (file.media_type || "").toLowerCase() === "video";
}

const REC_THRESHOLD_OPTIONS = [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8];

function matchSummary(suggestion: PersonClusterSuggestion): string {
  const percent = Math.round(suggestion.similarity * 100);
  const faces = `${suggestion.member_count} face${suggestion.member_count === 1 ? "" : "s"}`;
  const files = `${suggestion.file_count} file${suggestion.file_count === 1 ? "" : "s"}`;
  return `${percent}% match · ${faces} · ${files}`;
}

function ClusterFileTile({ file }: { file: ClusterFile }) {
  const video = isVideoFile(file);
  return (
    <a
      href={driveGoogleViewUrl(file.drive_file_id)}
      target="_blank"
      rel="noopener noreferrer"
      title={file.name}
      className="group block min-w-0 overflow-hidden rounded-lg border border-border bg-muted/40 transition-colors hover:border-ring"
    >
      <span className="relative block aspect-square">
        <DriveMediaThumb
          driveFileId={file.drive_file_id}
          name={file.name}
          isVideo={video}
          frameTimestamp={file.frame_timestamp}
        />
        {video && (
          <span className="absolute bottom-1 left-1 rounded bg-black/70 px-1 py-px text-[9px] font-semibold uppercase text-white">
            {file.frame_timestamp != null ? formatTimestamp(file.frame_timestamp) : "Video"}
          </span>
        )}
      </span>
      <span className="block truncate px-1.5 py-1 text-[10px] text-muted-foreground group-hover:text-foreground">
        {file.name}
      </span>
    </a>
  );
}

function ClusterGallery({
  files,
  faceIds,
  loading,
  expectedFileCount,
}: {
  files: ClusterFile[];
  faceIds: number[];
  loading: boolean;
  expectedFileCount: number;
}) {
  const photos = files.filter((file) => !isVideoFile(file));
  const videos = files.filter(isVideoFile);
  const sections = [
    { label: "Photos", items: photos },
    { label: "Videos", items: videos },
  ].filter((section) => section.items.length > 0);

  if (loading && files.length === 0 && faceIds.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        <LoadingLabel>Loading cluster…</LoadingLabel>
      </p>
    );
  }

  return (
    <div className="space-y-4">
      {faceIds.length > 0 && (
        <div>
          <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
            Faces in this cluster · {faceIds.length}
          </p>
          <ul className="flex flex-wrap gap-1.5">
            {faceIds.map((faceId) => (
              <li key={faceId}>
                <FaceThumb faceId={faceId} className="h-14 w-14 rounded-lg" />
              </li>
            ))}
          </ul>
        </div>
      )}
      {sections.map((section) => (
        <div key={section.label}>
          <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
            {section.label} · {section.items.length}
          </p>
          <ul className="grid grid-cols-[repeat(auto-fill,minmax(5.5rem,1fr))] gap-2">
            {section.items.map((file) => (
              <li key={file.media_id} className="min-w-0">
                <ClusterFileTile file={file} />
              </li>
            ))}
          </ul>
        </div>
      ))}
      {files.length === 0 && !loading && (
        <p className="text-sm text-muted-foreground">No Drive files linked to this cluster yet.</p>
      )}
      {expectedFileCount > files.length && files.length > 0 && (
        <p className="text-[11px] text-muted-foreground">
          Showing {files.length} of {expectedFileCount} files
        </p>
      )}
    </div>
  );
}

function SuggestionPicker({
  suggestions,
  selectedId,
  onSelect,
}: {
  suggestions: PersonClusterSuggestion[];
  selectedId: number | null;
  onSelect: (clusterId: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const selected = suggestions.find((item) => item.cluster_id === selectedId) ?? suggestions[0] ?? null;

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!selected) return null;

  return (
    <div ref={rootRef} className="relative w-full">
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className={cn(
          "flex h-11 w-full items-center gap-2.5 rounded-xl border border-border bg-background px-3 text-left text-sm shadow-sm outline-none transition-colors hover:border-muted-foreground/30 focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/20",
          open && "border-ring ring-2 ring-ring/20"
        )}
      >
        <FaceThumb
          faceId={selected.representative_face_id}
          className="h-7 w-7 shrink-0 rounded-full"
        />
        <span className="min-w-0 flex-1 truncate text-xs text-foreground">
          {matchSummary(selected)}
        </span>
        <span className="hidden items-center gap-1 sm:flex">
          {selected.sample_files.slice(0, 3).map((file) => (
            <span
              key={file.media_id}
              className="h-7 w-7 overflow-hidden rounded-md border border-border bg-muted/40"
            >
              <DriveMediaThumb
                driveFileId={file.drive_file_id}
                name={file.name}
                isVideo={isVideoFile(file)}
                frameTimestamp={file.frame_timestamp}
              />
            </span>
          ))}
        </span>
        <ChevronDown
          size={15}
          aria-hidden
          className={cn("shrink-0 text-muted-foreground transition-transform", open && "rotate-180")}
        />
      </button>
      {open && (
        <div className="absolute left-0 right-0 top-full z-40 mt-1.5 overflow-hidden rounded-xl border border-border bg-card py-1 shadow-lg">
          <ul role="listbox" aria-label="Potential matches" className="scrollbar-hidden max-h-80 overflow-y-auto">
            {suggestions.map((item) => {
              const isSel = item.cluster_id === selected.cluster_id;
              const percent = Math.round(item.similarity * 100);
              return (
                <li key={item.cluster_id}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={isSel}
                    onClick={() => {
                      onSelect(item.cluster_id);
                      setOpen(false);
                    }}
                    className={cn(
                      "flex w-full items-center gap-2.5 px-3 py-2 text-left text-xs transition-colors",
                      isSel
                        ? "bg-primary/10 font-medium text-primary"
                        : "text-foreground hover:bg-accent"
                    )}
                  >
                    <FaceThumb
                      faceId={item.representative_face_id}
                      className="h-8 w-8 shrink-0 rounded-full"
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate">
                        {percent}% match
                      </span>
                      <span className={cn("block truncate font-normal", isSel ? "text-primary/80" : "text-muted-foreground")}>
                        {item.member_count} face{item.member_count === 1 ? "" : "s"} · {item.file_count}{" "}
                        file{item.file_count === 1 ? "" : "s"}
                      </span>
                    </span>
                    <span className="flex shrink-0 items-center gap-1">
                      {item.sample_files.slice(0, 3).map((file) => (
                        <span
                          key={file.media_id}
                          className="h-8 w-8 overflow-hidden rounded-md border border-border bg-muted/40"
                        >
                          <DriveMediaThumb
                            driveFileId={file.drive_file_id}
                            name={file.name}
                            isVideo={isVideoFile(file)}
                            frameTimestamp={file.frame_timestamp}
                          />
                        </span>
                      ))}
                    </span>
                    {isSel && <Check size={13} className="shrink-0" aria-hidden />}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
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
  const [recThreshold, setRecThreshold] = useState(0.5);
  const [selectedClusterId, setSelectedClusterId] = useState<number | null>(null);
  const [clusterPreviews, setClusterPreviews] = useState<Record<number, ClusterPreview>>({});
  const [previewLoadingId, setPreviewLoadingId] = useState<number | null>(null);
  const savingRef = useRef(false);

  useEffect(() => {
    if (!id) return;
    apiClient.person(id).then((p) => {
      setPerson(p);
      setName(p.name);
    });
    apiClient.personMedia(id).then(setMedia);
  }, [id]);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    setSuggestionsLoading(true);
    apiClient
      .personClusterSuggestions(id, { limit: 12, offset: 0, minSimilarity: recThreshold })
      .then((result) => {
        if (cancelled) return;
        setSuggestions(result.items);
        setSuggestionTotal(result.total);
        setSuggestionError(null);
        setClusterPreviews({});
        setSelectedClusterId(result.items[0]?.cluster_id ?? null);
      })
      .catch((e) => {
        if (cancelled) return;
        setSuggestionError(formatApiError(e, "Could not load potential matches"));
      })
      .finally(() => {
        if (!cancelled) setSuggestionsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [id, recThreshold]);

  useEffect(() => {
    if (selectedClusterId == null) return;
    if (clusterPreviews[selectedClusterId]) return;
    let cancelled = false;
    const clusterId = selectedClusterId;
    const fallbackFiles =
      suggestions.find((item) => item.cluster_id === clusterId)?.sample_files ?? [];
    setPreviewLoadingId(clusterId);
    Promise.all([
      apiClient.cluster(clusterId).catch(() => null),
      apiClient.clusterFaces(clusterId, 24).catch(() => []),
    ])
      .then(([cluster, faces]) => {
        if (cancelled) return;
        setClusterPreviews((current) => ({
          ...current,
          [clusterId]: {
            files: cluster?.appears_in?.length ? cluster.appears_in : fallbackFiles,
            faceIds: faces.map((face) => face.id),
          },
        }));
      })
      .finally(() => {
        if (!cancelled) setPreviewLoadingId((current) => (current === clusterId ? null : current));
      });
    return () => {
      cancelled = true;
    };
  }, [selectedClusterId, suggestions]);

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
    const remaining = suggestions.filter((item) => item.cluster_id !== clusterId);
    setSuggestions(remaining);
    setSelectedClusterId((current) =>
      current === clusterId ? remaining[0]?.cluster_id ?? null : current
    );
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
      setSelectedClusterId(clusterId);
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
        minSimilarity: recThreshold,
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

  const selectedSuggestion =
    suggestions.find((item) => item.cluster_id === selectedClusterId) ?? suggestions[0] ?? null;
  const suggestionBusy =
    selectedSuggestion != null && suggestionActionId === selectedSuggestion.cluster_id;

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

      <Card className="relative min-w-0 overflow-visible p-0">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            <h3 className="font-medium">Potential matches</h3>
            <p className="text-xs text-muted-foreground">
              Unknown face clusters similar to {person.name}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              At least
              <select
                value={recThreshold}
                onChange={(e) => setRecThreshold(Number(e.target.value))}
                className="rounded-md border border-input bg-background px-1.5 py-0.5 text-[11px] font-medium text-foreground"
                aria-label="Minimum match similarity"
              >
                {REC_THRESHOLD_OPTIONS.map((value) => (
                  <option key={value} value={value}>
                    {Math.round(value * 100)}%
                  </option>
                ))}
              </select>
            </label>
            {suggestionTotal > 0 && (
              <span className="rounded-full bg-primary/10 px-2.5 py-1 text-xs font-semibold tabular-nums text-primary">
                {suggestionTotal} suggested
              </span>
            )}
          </div>
        </div>
        {suggestionsLoading ? (
          <p className="px-4 py-6 text-sm text-muted-foreground">
            <LoadingLabel>Finding matches…</LoadingLabel>
          </p>
        ) : suggestions.length === 0 ? (
          <p className="px-4 py-6 text-sm text-muted-foreground">
            No unreviewed clusters currently match this person at {Math.round(recThreshold * 100)}% or
            higher.
          </p>
        ) : (
          <div className="space-y-4 px-4 py-4">
            <SuggestionPicker
              suggestions={suggestions}
              selectedId={selectedClusterId}
              onSelect={setSelectedClusterId}
            />
            {selectedSuggestion && (
              <div className="space-y-4">
                <ClusterGallery
                  files={
                    clusterPreviews[selectedSuggestion.cluster_id]?.files ??
                    selectedSuggestion.sample_files
                  }
                  faceIds={
                    clusterPreviews[selectedSuggestion.cluster_id]?.faceIds ??
                    (selectedSuggestion.representative_face_id
                      ? [selectedSuggestion.representative_face_id]
                      : [])
                  }
                  loading={previewLoadingId === selectedSuggestion.cluster_id}
                  expectedFileCount={selectedSuggestion.file_count}
                />
                <div className="flex flex-wrap gap-2">
                  <Button
                    onClick={() => decideSuggestion(selectedSuggestion.cluster_id, "accept")}
                    disabled={suggestionActionId != null}
                    className="min-w-0"
                  >
                    <Check size={15} aria-hidden />
                    {suggestionBusy ? "Saving…" : `Add to ${person.name}`}
                  </Button>
                  <Button
                    variant="secondary"
                    onClick={() => decideSuggestion(selectedSuggestion.cluster_id, "reject")}
                    disabled={suggestionActionId != null}
                    title={`This cluster is not ${person.name}`}
                  >
                    <X size={15} aria-hidden />
                    Not {person.name}
                  </Button>
                </div>
              </div>
            )}
          </div>
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
                        <>
                          <DriveMediaThumb
                            driveFileId={m.drive_file_id}
                            name={m.name}
                            isVideo
                            frameTimestamp={m.frame_timestamp}
                          />
                          <span className="absolute bottom-0.5 left-0.5 rounded bg-black/70 px-1 py-px text-[8px] font-semibold uppercase text-white">
                            {m.frame_timestamp != null ? formatTimestamp(m.frame_timestamp) : "Video"}
                          </span>
                        </>
                      ) : (
                        <DriveMediaThumb driveFileId={m.drive_file_id} name={m.name} />
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
