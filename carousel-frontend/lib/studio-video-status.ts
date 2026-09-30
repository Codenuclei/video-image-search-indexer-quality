/** Human-readable upload → index → caption status for /test/studio. */

import { driveFolderPath } from "./drive-path";

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

/** Lower-cased display names that appear more than once in a video list. */
export function duplicateVideoNames(videos: { name: string }[]): Set<string> {
  const counts = new Map<string, number>();
  for (const v of videos) {
    const key = (v.name || "").trim().toLowerCase();
    if (!key) continue;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const dupes = new Set<string>();
  for (const [key, n] of counts) if (n > 1) dupes.add(key);
  return dupes;
}

export type DedupeVideo = {
  id: string;
  name: string;
  path?: string | null;
  status?: string | null;
  has_captions?: boolean;
  cue_count?: number | null;
};

/** Prefer captioned / higher cue count / further along in the index pipeline. */
export function studioVideoRank(video: DedupeVideo): number {
  const status = (video.status || "").trim().toLowerCase();
  const cues = video.cue_count ?? 0;
  const captioned = Boolean(video.has_captions || cues > 0);
  if (captioned) return 1000 + Math.min(cues, 500);
  if (status === "processed") return 500;
  if (status === "processing") return 200;
  if (status === "pending") return 100;
  if (status === "error" || status === "skipped") return 0;
  return 50;
}

/**
 * Collapse same display name to a single row (re-indexed copies of the same talk).
 * Keeps the best ranked row; preserves first-seen order of winning names.
 * Pass `preferId` to always keep that id when it shares a name with others.
 */
export function dedupeVideosByName<T extends DedupeVideo>(
  videos: T[],
  opts?: { preferId?: string | null }
): T[] {
  const preferId = opts?.preferId || null;
  const bestByName = new Map<string, T>();
  for (const video of videos) {
    const key = (video.name || "").trim().toLowerCase();
    if (!key) {
      continue;
    }
    const prev = bestByName.get(key);
    if (!prev) {
      bestByName.set(key, video);
      continue;
    }
    if (preferId && video.id === preferId) {
      bestByName.set(key, video);
      continue;
    }
    if (preferId && prev.id === preferId) continue;
    if (studioVideoRank(video) > studioVideoRank(prev)) {
      bestByName.set(key, video);
    }
  }
  const seen = new Set<string>();
  const out: T[] = [];
  for (const video of videos) {
    const key = (video.name || "").trim().toLowerCase();
    if (!key) {
      if (!seen.has(video.id)) {
        seen.add(video.id);
        out.push(video);
      }
      continue;
    }
    const winner = bestByName.get(key);
    if (!winner || seen.has(key)) continue;
    seen.add(key);
    out.push(winner);
  }
  return out;
}

/**
 * Distinguishing subtitle for videos that share a name: parent folder path when
 * known, otherwise a short id suffix.
 */
export function videoDisambiguator(video: {
  id: string;
  name: string;
  path?: string | null;
}): string {
  const folder = driveFolderPath(video.path, video.name);
  const suffix = `id …${video.id.slice(-6)}`;
  return folder ? `${folder} · ${suffix}` : suffix;
}
