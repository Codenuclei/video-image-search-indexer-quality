/** Human-readable upload → index → caption status for /test/studio. */

export type StudioVideoStatusInput = {
  status?: string | null;
  has_captions?: boolean;
  cue_count?: number | null;
};

export function studioVideoStatus(video: StudioVideoStatusInput): {
  label: string;
  tone: string;
  inflight: boolean;
} {
  const status = (video.status || "").trim().toLowerCase();
  const cues = video.cue_count ?? 0;
  const captioned = Boolean(video.has_captions || cues > 0);
  if (status === "error") {
    return { label: "Couldn’t index", tone: "text-red-700", inflight: false };
  }
  if (status === "skipped") {
    return { label: "Skipped", tone: "text-slate-500", inflight: false };
  }
  if (status === "pending") {
    return { label: "Uploaded — waiting to index", tone: "text-amber-700", inflight: true };
  }
  if (status === "processing") {
    return { label: "Indexing…", tone: "text-blue-700", inflight: true };
  }
  if (status === "processed" && !captioned) {
    return { label: "Getting captions…", tone: "text-blue-700", inflight: true };
  }
  if (captioned) {
    return { label: `Ready · ${cues} cue${cues === 1 ? "" : "s"}`, tone: "text-emerald-700", inflight: false };
  }
  return { label: status || "In library", tone: "text-slate-500", inflight: false };
}
