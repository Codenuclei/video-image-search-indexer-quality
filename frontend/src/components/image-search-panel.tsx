"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  ChevronRight,
  ExternalLink,
  FileVideo,
  ImagePlus,
  Linkedin,
  X,
} from "lucide-react";
import {
  apiClient,
  driveFileThumbnailUrl,
  driveGoogleViewUrl,
  formatApiError,
  type FaceSearchAppearance,
  type FaceSearchMatch,
  type Person,
} from "@/lib/api";
import { Button, ConfirmDialog, FaceThumb, Input, LoadingLabel, Spinner } from "@/components/ui";
import { ModalOverlay } from "@/components/modal";
import { PersonMergeSearch } from "@/components/person-merge-search";
import { cn } from "@/lib/utils";
import {
  clearReverseFaceSearch,
  collectUnknownNameTagIds,
  isUnknownFaceMatch,
  patchReverseFaceSession,
  runReverseFaceNameTag,
  runReverseFaceSearch,
  setReverseFaceFile,
  totalMatchFileCount,
  useReverseFaceSession,
} from "@/lib/reverse-face-session";

const APPEARANCES_PAGE_SIZE = 60;

type MatchScope = "cluster" | "person" | "face";

function scopeOf(match: FaceSearchMatch): MatchScope {
  if (match.match_scope) return match.match_scope;
  if (match.cluster_id != null) return "cluster";
  if (match.person_id != null) return "person";
  return "face";
}

function isVideo(item: FaceSearchAppearance) {
  return item.media_type.toLowerCase() === "video";
}

function statusBadgeClass(status: string) {
  if (status === "named") return "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300";
  if (status === "ignored") return "bg-muted text-muted-foreground";
  return "bg-amber-500/15 text-amber-700 dark:text-amber-300";
}

function usePersonBase() {
  const pathname = usePathname();
  return pathname.startsWith("/test") ? "/test/people" : "/people";
}

function ClusterRow({ match, onOpen }: { match: FaceSearchMatch; onOpen: () => void }) {
  const personBase = usePersonBase();
  const scope = scopeOf(match);
  const status = (match.cluster_status ?? (match.person_id != null ? "named" : "unknown")).toLowerCase();
  const faceCount = match.cluster_member_count;
  const fileCount = match.file_count ?? match.appears_in?.length ?? 0;
  const profileHref = match.person_id != null ? `${personBase}/${match.person_id}` : null;
  const titled =
    scope === "cluster"
      ? `Cluster #${match.cluster_id}`
      : scope === "person"
        ? match.person_name
        : "Unlinked face";

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
      className="flex w-full cursor-pointer items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <FaceThumb faceId={match.thumb_face_id ?? match.face_id} className="h-12 w-12 shrink-0 rounded-lg" />
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm leading-snug">
          {scope === "cluster" && match.person_id != null && profileHref ? (
            <Link
              href={profileHref}
              onClick={(e) => e.stopPropagation()}
              className="truncate font-semibold text-foreground underline-offset-2 hover:underline"
            >
              {match.person_name}
            </Link>
          ) : (
            <span className="truncate font-semibold text-foreground">{titled}</span>
          )}
          <span
            className={cn(
              "rounded-full px-1.5 py-0.5 text-[10px] font-semibold",
              statusBadgeClass(status)
            )}
          >
            {status}
          </span>
        </p>
        <p className="mt-0.5 truncate text-[11px] text-muted-foreground">
          {scope === "cluster" && match.person_id != null ? `cluster #${match.cluster_id} · ` : ""}
          {Math.round(match.score * 100)}% similar
          {faceCount != null ? ` · ${faceCount} face${faceCount === 1 ? "" : "s"}` : ""}
          {fileCount > 0 ? ` · ${fileCount} file${fileCount === 1 ? "" : "s"}` : ""}
        </p>
      </div>
      <span className="flex shrink-0 items-center gap-1">
        {match.linkedin_url && (
          <a
            href={match.linkedin_url}
            target="_blank"
            rel="noopener noreferrer"
            title="LinkedIn"
            onClick={(e) => e.stopPropagation()}
            className="flex h-7 w-7 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
          >
            <Linkedin size={13} />
          </a>
        )}
        {profileHref && scope !== "cluster" && (
          <Link
            href={profileHref}
            title="Profile"
            onClick={(e) => e.stopPropagation()}
            className="flex h-7 w-7 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
          >
            <ExternalLink size={13} />
          </Link>
        )}
        <ChevronRight size={15} className="text-muted-foreground" aria-hidden />
      </span>
    </div>
  );
}

function ClusterGalleryDialog({
  match,
  tagging,
  onClose,
}: {
  match: FaceSearchMatch;
  tagging: boolean;
  onClose: () => void;
}) {
  const personBase = usePersonBase();
  const scope = scopeOf(match);
  const status = (match.cluster_status ?? (match.person_id != null ? "named" : "unknown")).toLowerCase();
  const unknown = isUnknownFaceMatch(match);
  const profileHref = match.person_id != null ? `${personBase}/${match.person_id}` : null;

  const [items, setItems] = useState<FaceSearchAppearance[]>(match.appears_in ?? []);
  const [loadingMore, setLoadingMore] = useState(false);
  const [draft, setDraft] = useState("");
  const [naming, setNaming] = useState(false);
  const [merging, setMerging] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const total = match.file_count ?? items.length;
  const pageKey =
    scope === "cluster" && match.cluster_id != null
      ? { clusterId: match.cluster_id }
      : scope === "person" && match.person_id != null
        ? { personId: match.person_id }
        : null;
  const canLoadMore = pageKey != null && items.length < total;

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const sortedItems = useMemo(() => {
    const images = items.filter((item) => !isVideo(item));
    const videos = items.filter(isVideo);
    return [...images, ...videos];
  }, [items]);
  const firstVideoId = sortedItems.find(isVideo)?.drive_file_id ?? null;

  async function loadMore() {
    if (!pageKey || loadingMore || !canLoadMore) return;
    setLoadingMore(true);
    try {
      const page = await apiClient.faceMatchAppearances({
        ...pageKey,
        offset: items.length,
        limit: APPEARANCES_PAGE_SIZE,
      });
      setItems((current) => {
        const seen = new Set(current.map((item) => item.drive_file_id));
        return [...current, ...page.items.filter((item) => !seen.has(item.drive_file_id))];
      });
    } finally {
      setLoadingMore(false);
    }
  }

  async function submitName() {
    const name = draft.trim();
    if (!name || naming || tagging) return;
    setNaming(true);
    setActionError(null);
    try {
      const clusterIds = match.cluster_id != null ? [match.cluster_id] : [];
      const faceIds = clusterIds.length === 0 && match.person_id == null ? [match.face_id] : [];
      const ok = await runReverseFaceNameTag({ name, clusterIds, faceIds });
      if (ok) onClose();
    } finally {
      setNaming(false);
    }
  }

  async function mergeInto(person: Person) {
    if (match.cluster_id == null || merging) return;
    setMerging(true);
    setActionError(null);
    try {
      await apiClient.mergeCluster(match.cluster_id, person.id);
      patchReverseFaceSession({
        tagMessage: `Merged cluster #${match.cluster_id} into ${person.name}.`,
      });
      await runReverseFaceSearch();
      onClose();
    } catch (e) {
      setActionError(formatApiError(e, "Could not merge this cluster"));
    } finally {
      setMerging(false);
    }
  }

  const title =
    scope === "cluster"
      ? match.person_id != null
        ? match.person_name
        : `Cluster #${match.cluster_id}`
      : scope === "person"
        ? match.person_name
        : "Unlinked face";

  return (
    <ModalOverlay open onClose={onClose} contentClassName="max-w-[min(92vw,52rem)]">
      <div className="flex max-h-[85dvh] flex-col rounded-xl bg-card shadow-2xl">
        <div className="flex shrink-0 items-center gap-3 rounded-t-xl border-b border-border px-4 py-3">
          <FaceThumb faceId={match.thumb_face_id ?? match.face_id} className="h-14 w-14 shrink-0 rounded-lg" />
          <div className="min-w-0 flex-1">
            <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
              <span className="truncate text-sm font-semibold text-foreground">{title}</span>
              <span
                className={cn(
                  "rounded-full px-1.5 py-0.5 text-[10px] font-semibold",
                  statusBadgeClass(status)
                )}
              >
                {status}
              </span>
            </p>
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              {scope === "cluster" ? `Cluster #${match.cluster_id} · ` : ""}
              {Math.round(match.score * 100)}% similar
              {match.cluster_member_count != null
                ? ` · ${match.cluster_member_count} face${match.cluster_member_count === 1 ? "" : "s"}`
                : ""}
              {total > 0 ? ` · ${total} file${total === 1 ? "" : "s"}` : ""}
            </p>
            {(profileHref || match.linkedin_url) && (
              <p className="mt-1 flex items-center gap-3 text-[11px]">
                {profileHref && (
                  <Link
                    href={profileHref}
                    className="inline-flex items-center gap-1 font-medium text-sky-700 underline-offset-2 hover:underline dark:text-sky-300"
                  >
                    Profile
                    <ExternalLink size={10} aria-hidden />
                  </Link>
                )}
                {match.linkedin_url && (
                  <a
                    href={match.linkedin_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 font-medium text-sky-700 underline-offset-2 hover:underline dark:text-sky-300"
                  >
                    LinkedIn
                    <Linkedin size={10} aria-hidden />
                  </a>
                )}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="flex h-8 w-8 shrink-0 items-center justify-center self-start rounded-full border border-border bg-card text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <X size={15} aria-hidden />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          {sortedItems.length === 0 ? (
            <p className="py-6 text-center text-xs text-muted-foreground">
              No Drive files linked to this {scope === "cluster" ? "cluster" : "match"} yet.
            </p>
          ) : (
            <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5">
              {sortedItems.map((item) => (
                <div key={item.drive_file_id} className="contents">
                  {firstVideoId === item.drive_file_id && (
                    <p className="col-span-full pt-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                      Videos
                    </p>
                  )}
                  <a
                    href={driveGoogleViewUrl(item.drive_file_id)}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={`${item.name}\n${item.path || item.media_type}`}
                    className="group block overflow-hidden rounded-lg border border-border bg-muted transition-colors hover:border-ring"
                  >
                    <span className="relative block aspect-square">
                      {isVideo(item) ? (
                        <>
                          <span className="flex h-full items-center justify-center text-muted-foreground">
                            <FileVideo size={22} />
                          </span>
                          <span className="absolute bottom-1 left-1 rounded bg-black/70 px-1 py-0.5 text-[9px] font-semibold uppercase text-white">
                            Video
                          </span>
                        </>
                      ) : (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img
                          src={driveFileThumbnailUrl(item.drive_file_id)}
                          alt={item.name}
                          loading="lazy"
                          className="h-full w-full object-cover"
                        />
                      )}
                    </span>
                    <span className="block truncate px-1.5 py-1 text-[10px] text-muted-foreground group-hover:text-foreground">
                      {item.name}
                    </span>
                  </a>
                </div>
              ))}
            </div>
          )}
          {canLoadMore && (
            <div className="mt-3 flex justify-center">
              <Button
                variant="secondary"
                onClick={() => void loadMore()}
                disabled={loadingMore}
                className="px-3 py-1.5 text-xs"
              >
                {loadingMore ? (
                  <LoadingLabel>Loading files…</LoadingLabel>
                ) : (
                  `Load more (${items.length} of ${total})`
                )}
              </Button>
            </div>
          )}
        </div>

        {unknown && (
          <div className="shrink-0 space-y-2 rounded-b-xl border-t border-border px-4 py-3">
            <div className="flex items-center gap-2">
              <Input
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="Name this person"
                className="h-8 text-xs"
                disabled={naming || tagging || merging}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void submitName();
                }}
              />
              <Button
                type="button"
                className="h-8 shrink-0 px-3 text-xs"
                disabled={!draft.trim() || naming || tagging || merging}
                onClick={() => void submitName()}
              >
                {naming ? <Spinner size={12} /> : "Name"}
              </Button>
            </div>
            {match.cluster_id != null && (
              <div className="flex items-center gap-2">
                <span className="shrink-0 text-[11px] text-muted-foreground">or merge into</span>
                <PersonMergeSearch
                  disabled={naming || tagging}
                  busy={merging}
                  busyLabel="Merging cluster…"
                  onSelect={(person) => void mergeInto(person)}
                />
              </div>
            )}
            {actionError && <p className="text-xs text-destructive">{actionError}</p>}
          </div>
        )}
      </div>
    </ModalOverlay>
  );
}

export function ImageSearchPanel() {
  const { file, previewUrl, searching, result, error, tagging, tagMessage } = useReverseFaceSession();
  const uploadRef = useRef<HTMLInputElement>(null);
  const [bulkName, setBulkName] = useState("");
  const [confirmBulkOpen, setConfirmBulkOpen] = useState(false);
  const [openMatchKey, setOpenMatchKey] = useState<string | null>(null);

  const matches = useMemo(() => result?.matches ?? [], [result?.matches]);
  const unknownIds = useMemo(() => collectUnknownNameTagIds(matches), [matches]);
  const unknownCount = unknownIds.clusterIds.length + unknownIds.faceIds.length;
  const matchFileCount = useMemo(() => totalMatchFileCount(matches), [matches]);
  const clusterCount = useMemo(
    () => matches.filter((m) => scopeOf(m) === "cluster").length,
    [matches]
  );
  const openMatch = useMemo(() => {
    if (openMatchKey == null) return null;
    return (
      matches.find(
        (m) => `${scopeOf(m)}:${m.cluster_id ?? m.person_id ?? m.face_id}` === openMatchKey
      ) ?? null
    );
  }, [matches, openMatchKey]);

  function onPick(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setOpenMatchKey(null);
    setReverseFaceFile(file);
    void runReverseFaceSearch(file);
  }

  async function applyBulkName() {
    const name = bulkName.trim();
    if (!name || unknownCount === 0) return;
    setConfirmBulkOpen(false);
    const ok = await runReverseFaceNameTag({
      name,
      clusterIds: unknownIds.clusterIds,
      faceIds: unknownIds.faceIds,
    });
    if (ok) setBulkName("");
  }

  return (
    <div className="space-y-4">
      <input ref={uploadRef} type="file" accept="image/*" className="hidden" onChange={onPick} />

      <div className="flex items-center gap-4">
        <button
          type="button"
          onClick={() => uploadRef.current?.click()}
          title="Upload an image"
          className="group relative flex h-20 w-20 shrink-0 items-center justify-center overflow-hidden rounded-xl border border-dashed border-border bg-muted/40 text-muted-foreground transition-colors hover:border-ring hover:text-foreground"
        >
          {previewUrl ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={previewUrl} alt="Query" className="h-full w-full object-cover" />
          ) : (
            <ImagePlus size={22} />
          )}
          <span className="absolute inset-0 flex items-center justify-center bg-black/50 opacity-0 transition-opacity group-hover:opacity-100">
            <ImagePlus size={18} className="text-white" />
          </span>
        </button>
        <div className="min-w-0 flex-1 text-xs text-muted-foreground">
          {searching ? (
            <LoadingLabel size={14}>Searching faces…</LoadingLabel>
          ) : result ? (
            <p>
              {result.faces_detected} face{result.faces_detected === 1 ? "" : "s"} detected
              {matches.length > 0
                ? clusterCount > 0
                  ? ` · ${clusterCount} cluster${clusterCount === 1 ? "" : "s"}${
                      matches.length > clusterCount
                        ? ` + ${matches.length - clusterCount} other match${
                            matches.length - clusterCount === 1 ? "" : "es"
                          }`
                        : ""
                    }`
                  : ` · ${matches.length} match${matches.length === 1 ? "" : "es"}`
                : " · no matches"}
              {matchFileCount > 0
                ? ` · ${matchFileCount} file${matchFileCount === 1 ? "" : "s"}`
                : ""}
              {unknownCount > 0
                ? ` · ${unknownCount} unknown to name`
                : matches.length > 0
                  ? " · all named"
                  : ""}
            </p>
          ) : (
            <p>Upload a photo to find matching people across your library.</p>
          )}
          {error && <p className="mt-1 text-red-600 dark:text-red-400">{error}</p>}
          {tagMessage && <p className="mt-1 text-emerald-700 dark:text-emerald-400">{tagMessage}</p>}
        </div>
        {(searching || tagging) && <Spinner size={16} />}
        {(file || result) && (
          <button
            type="button"
            onClick={() => {
              setOpenMatchKey(null);
              clearReverseFaceSearch();
            }}
            title="Clear image search"
            aria-label="Clear image search"
            className="flex h-8 w-8 shrink-0 items-center justify-center self-start rounded-full border border-border bg-card text-muted-foreground shadow-sm transition-colors hover:bg-muted hover:text-foreground"
          >
            <X size={15} aria-hidden />
          </button>
        )}
      </div>

      {unknownCount > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border/60 bg-muted/20 p-3">
          <Input
            value={bulkName}
            onChange={(e) => setBulkName(e.target.value)}
            placeholder={`Name all ${unknownCount} unknown${unknownCount === 1 ? "" : "s"}`}
            className="h-8 min-w-[12rem] flex-1 text-xs"
            disabled={tagging}
            onKeyDown={(e) => {
              if (e.key === "Enter" && bulkName.trim()) setConfirmBulkOpen(true);
            }}
          />
          <Button
            type="button"
            className="h-8 px-3 text-xs"
            disabled={!bulkName.trim() || tagging}
            onClick={() => setConfirmBulkOpen(true)}
          >
            Name all unknowns ({unknownCount})
          </Button>
        </div>
      )}

      {matches.length > 0 && (
        <div className="divide-y divide-border overflow-hidden rounded-xl border border-border/60 bg-card">
          {matches.map((m) => {
            const key = `${scopeOf(m)}:${m.cluster_id ?? m.person_id ?? m.face_id}`;
            return <ClusterRow key={`${key}-${m.face_id}`} match={m} onOpen={() => setOpenMatchKey(key)} />;
          })}
        </div>
      )}

      {openMatch && (
        <ClusterGalleryDialog
          match={openMatch}
          tagging={tagging}
          onClose={() => setOpenMatchKey(null)}
        />
      )}

      <ConfirmDialog
        open={confirmBulkOpen}
        title="Name all unknowns"
        message={`Apply “${bulkName.trim()}” to ${unknownCount} unknown match${unknownCount === 1 ? "" : "es"}?`}
        confirmLabel={`Name ${unknownCount}`}
        onConfirm={() => void applyBulkName()}
        onCancel={() => setConfirmBulkOpen(false)}
      />
    </div>
  );
}
