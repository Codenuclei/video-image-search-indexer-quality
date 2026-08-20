"use client";

import { useRef } from "react";
import Link from "next/link";
import { ExternalLink, ImagePlus, Linkedin } from "lucide-react";
import { driveGoogleViewUrl, type FaceSearchMatch } from "@/lib/api";
import { FaceThumb, LoadingLabel, Spinner } from "@/components/ui";
import {
  runReverseFaceSearch,
  setReverseFaceFile,
  useReverseFaceSession,
} from "@/lib/reverse-face-session";

function MatchCard({ match }: { match: FaceSearchMatch }) {
  const appearances = match.appears_in ?? [];
  return (
    <div className="rounded-xl border border-border/60 bg-muted/20 p-3">
      <div className="flex items-center gap-3">
        <FaceThumb faceId={match.face_id} className="h-12 w-12 shrink-0 rounded-lg" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold text-foreground">{match.person_name}</p>
          <p className="text-[11px] text-muted-foreground">
            {Math.round(match.score * 100)}% match
            {match.cluster_id != null ? ` · cluster #${match.cluster_id}` : ""}
            {appearances.length > 0 ? ` · ${appearances.length} file${appearances.length === 1 ? "" : "s"}` : ""}
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
              href={`/people/${match.person_id}`}
              title="Profile"
              className="flex h-7 w-7 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            >
              <ExternalLink size={13} />
            </Link>
          )}
        </div>
      </div>
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
  const { previewUrl, searching, result, error } = useReverseFaceSession();
  const uploadRef = useRef<HTMLInputElement>(null);

  function onPick(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setReverseFaceFile(file);
    void runReverseFaceSearch(file);
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
        <div className="min-w-0 text-xs text-muted-foreground">
          {searching ? (
            <LoadingLabel size={14}>Searching faces…</LoadingLabel>
          ) : result ? (
            <p>
              {result.faces_detected} face{result.faces_detected === 1 ? "" : "s"} detected
              {result.matches.length > 0
                ? ` · ${result.matches.length} match${result.matches.length === 1 ? "" : "es"}`
                : " · no matches"}
            </p>
          ) : (
            <p>Upload a photo to find matching people across your library.</p>
          )}
          {error && <p className="mt-1 text-red-600 dark:text-red-400">{error}</p>}
        </div>
        {searching && <Spinner size={16} />}
      </div>

      {result && result.matches.length > 0 && (
        <div className="grid gap-2 sm:grid-cols-2">
          {result.matches.map((m) => (
            <MatchCard key={m.face_id} match={m} />
          ))}
        </div>
      )}
    </div>
  );
}
