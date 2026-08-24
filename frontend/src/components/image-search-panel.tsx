"use client";

import { useMemo, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ExternalLink, ImagePlus, Linkedin, X } from "lucide-react";
import { driveGoogleViewUrl, type FaceSearchMatch } from "@/lib/api";
import { Button, ConfirmDialog, FaceThumb, Input, LoadingLabel, Spinner } from "@/components/ui";
import {
  clearReverseFaceSearch,
  collectUnknownNameTagIds,
  isUnknownFaceMatch,
  runReverseFaceNameTag,
  runReverseFaceSearch,
  setReverseFaceFile,
  totalMatchFileCount,
  useReverseFaceSession,
} from "@/lib/reverse-face-session";

function MatchCard({ match, tagging }: { match: FaceSearchMatch; tagging: boolean }) {
  const appearances = match.appears_in ?? [];
  const fileCount = match.file_count ?? appearances.length;
  const unknown = isUnknownFaceMatch(match);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const pathname = usePathname();
  const personBase = pathname.startsWith("/test") ? "/test/people" : "/people";

  async function submitName() {
    const name = draft.trim();
    if (!name || busy || tagging) return;
    setBusy(true);
    try {
      const clusterIds =
        match.cluster_id != null && unknown ? [match.cluster_id] : [];
      const faceIds =
        clusterIds.length === 0 && match.person_id == null ? [match.face_id] : [];
      const ok = await runReverseFaceNameTag({ name, clusterIds, faceIds });
      if (ok) setDraft("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-xl border border-border/60 bg-muted/20 p-3">
      <div className="flex items-center gap-3">
        <FaceThumb faceId={match.face_id} className="h-12 w-12 shrink-0 rounded-lg" />
        <div className="min-w-0 flex-1">
          <p className="break-words text-sm font-semibold leading-snug text-foreground">{match.person_name}</p>
          <p className="text-[11px] text-muted-foreground">
            {Math.round(match.score * 100)}% match
            {match.cluster_id != null ? ` · cluster #${match.cluster_id}` : ""}
            {fileCount > 0
              ? ` · ${fileCount} file${fileCount === 1 ? "" : "s"}`
              : ""}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {match.linkedin_url && (
            <a
              href={match.linkedin_url}
              target="_blank"
              rel="noopener noreferrer"
              title="LinkedIn"
              className="flex h-7 w-7 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            >
              <Linkedin size={13} />
            </a>
          )}
          {match.person_id != null && (
            <Link
              href={`${personBase}/${match.person_id}`}
              title="Profile"
              className="flex h-7 w-7 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            >
              <ExternalLink size={13} />
            </Link>
          )}
        </div>
      </div>
      {unknown && (
        <div className="mt-2 flex items-center gap-2">
          <Input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Name this person"
            className="h-8 text-xs"
            disabled={busy || tagging}
            onKeyDown={(e) => {
              if (e.key === "Enter") void submitName();
            }}
          />
          <Button
            type="button"
            className="h-8 shrink-0 px-3 text-xs"
            disabled={!draft.trim() || busy || tagging}
            onClick={() => void submitName()}
          >
            {busy ? <Spinner size={12} /> : "Name"}
          </Button>
        </div>
      )}
      {appearances.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {appearances.slice(0, 6).map((a) => (
            <a
              key={`${a.drive_file_id}-${a.frame_timestamp ?? 0}`}
              href={driveGoogleViewUrl(a.drive_file_id)}
              target="_blank"
              rel="noopener noreferrer"
              title={a.path}
              className="max-w-[10rem] truncate rounded-full border border-border bg-card px-2 py-0.5 text-[10px] text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            >
              {a.name}
            </a>
          ))}
          {appearances.length > 6 && (
            <span className="px-1 py-0.5 text-[10px] text-muted-foreground">
              +{appearances.length - 6}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

export function ImageSearchPanel() {
  const { file, previewUrl, searching, result, error, tagging, tagMessage } = useReverseFaceSession();
  const uploadRef = useRef<HTMLInputElement>(null);
  const [bulkName, setBulkName] = useState("");
  const [confirmBulkOpen, setConfirmBulkOpen] = useState(false);

  const unknownIds = useMemo(
    () => collectUnknownNameTagIds(result?.matches ?? []),
    [result?.matches]
  );
  const unknownCount = unknownIds.clusterIds.length + unknownIds.faceIds.length;
  const matchFileCount = useMemo(
    () => totalMatchFileCount(result?.matches ?? []),
    [result?.matches]
  );

  function onPick(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
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
              {matchFileCount > 0
                ? ` · ${matchFileCount} match${matchFileCount === 1 ? "" : "es"}`
                : result.matches.length > 0
                  ? ` · ${result.matches.length} match${result.matches.length === 1 ? "" : "es"}`
                  : " · no matches"}
              {unknownCount > 0
                ? ` · ${unknownCount} unknown to name`
                : result.matches.length > 0
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
            onClick={clearReverseFaceSearch}
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

      {result && result.matches.length > 0 && (
        <div className="grid gap-2 sm:grid-cols-2">
          {result.matches.map((m) => (
            <MatchCard key={m.face_id} match={m} tagging={tagging} />
          ))}
        </div>
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
