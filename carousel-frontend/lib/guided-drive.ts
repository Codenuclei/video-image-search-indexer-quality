/** Guided Drive panel state helpers (frontend-only). */

export type GuidedDriveSessionLike = {
  connected?: boolean;
  email?: string | null;
  selected_folder?: { id: string; name: string } | null;
};

export type GuidedPrimaryAction =
  | { kind: "connect"; label: string }
  | { kind: "choose_folder"; label: string }
  | { kind: "choose_videos"; label: string };

export function guidedPrimaryAction(
  session: GuidedDriveSessionLike | null | undefined
): GuidedPrimaryAction {
  if (!session?.connected) {
    return { kind: "connect", label: "Connect Google Drive" };
  }
  if (!session.selected_folder?.id) {
    return { kind: "choose_folder", label: "Choose a folder" };
  }
  return { kind: "choose_videos", label: "Choose videos to index" };
}

export type GuidedIndexItemOutcome = {
  drive_file_id: string;
  name?: string;
  status?: string;
  queued?: boolean;
  has_captions?: boolean;
  cue_count?: number;
  ok?: boolean;
};

export function guidedIndexSummaryNote(opts: {
  requestCount: number;
  items: GuidedIndexItemOutcome[];
  skippedNeedsIndex?: number;
}): string {
  const ready = opts.items.filter(
    (it) =>
      it.ok !== false &&
      (it.status || "").toLowerCase() === "processed" &&
      (it.has_captions || (it.cue_count ?? 0) > 0) &&
      !it.queued
  ).length;
  const queued = opts.items.filter(
    (it) => it.ok !== false && (it.queued || (it.status || "").toLowerCase() !== "processed")
  ).length;
  const failed = opts.items.filter((it) => it.ok === false).length;

  const parts: string[] = [];
  if (queued > 0) {
    parts.push(
      queued === 1
        ? "1 video queued for indexing"
        : `${queued} videos queued for indexing`
    );
  }
  if (ready > 0) {
    parts.push(
      ready === 1 ? "1 already ready" : `${ready} already ready`
    );
  }
  if (failed > 0) {
    parts.push(failed === 1 ? "1 failed" : `${failed} failed`);
  }
  if (!parts.length) {
    parts.push(
      opts.requestCount === 1
        ? "1 video submitted"
        : `${opts.requestCount} videos submitted`
    );
  }
  let note = parts.join(" · ") + ".";
  if (opts.skippedNeedsIndex && opts.skippedNeedsIndex > 0) {
    note += ` Skipped ${opts.skippedNeedsIndex} not-yet-indexed video(s) — reconnect Drive to index those.`;
  }
  return note;
}

export function guidedFolderSaveNote(
  folderName: string,
  reusedExistingIndex?: boolean
): string {
  if (reusedExistingIndex) {
    return `Existing index ready for “${folderName}”.`;
  }
  return `Folder sync started for “${folderName}”.`;
}

export function guidedProgressLabel(input: {
  status?: string | null;
  has_captions?: boolean;
  cue_count?: number | null;
  queued?: boolean;
}): { label: string; tone: string; inflight: boolean } {
  const status = (input.status || "").trim().toLowerCase();
  const cues = input.cue_count ?? 0;
  const captioned = Boolean(input.has_captions || cues > 0);
  if (status === "error") {
    return { label: "Failed", tone: "text-red-700", inflight: false };
  }
  if (status === "skipped") {
    return { label: "Skipped", tone: "text-slate-500", inflight: false };
  }
  if (status === "pending" || input.queued) {
    return { label: "Queued", tone: "text-amber-700", inflight: true };
  }
  if (status === "processing") {
    return { label: "Downloading/transcribing…", tone: "text-blue-700", inflight: true };
  }
  if (status === "processed" && !captioned) {
    return { label: "Downloading/transcribing…", tone: "text-blue-700", inflight: true };
  }
  if (captioned) {
    return {
      label: `Ready · ${cues} cue${cues === 1 ? "" : "s"}`,
      tone: "text-emerald-700",
      inflight: false,
    };
  }
  return { label: status || "In library", tone: "text-slate-500", inflight: false };
}
