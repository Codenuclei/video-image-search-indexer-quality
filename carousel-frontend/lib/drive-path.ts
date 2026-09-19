/** Open-in-Drive URL for a library file id. */
export function driveFileOpenUrl(fileId: string): string {
  return `https://drive.google.com/file/d/${encodeURIComponent(fileId)}/view`;
}

/** Parent folder path from a stored Drive path (strips the trailing file name). */
export function driveFolderPath(
  path: string | null | undefined,
  name: string | null | undefined
): string {
  const full = (path || "").trim().replace(/\/+$/, "");
  const base = (name || "").trim();
  if (!full) return "";
  if (base && (full === base || full.endsWith(`/${base}`))) {
    const parent = full === base ? "" : full.slice(0, -(base.length + 1));
    return parent.replace(/\/+$/, "");
  }
  const slash = full.lastIndexOf("/");
  return slash >= 0 ? full.slice(0, slash) : "";
}

/** Human-readable status line, including skip reasons when present. */
export function driveFileStatusLabel(
  status: string | null | undefined,
  errorMessage?: string | null
): string {
  const key = (status || "").trim().toLowerCase();
  const err = (errorMessage || "").trim();
  if (key === "pending") return "Waiting to index";
  if (key === "processing") return "Indexing…";
  if (key === "processed") return "Ready";
  if (key === "error") return "Couldn’t index";
  if (key === "skipped") {
    if (err.toLowerCase().startsWith("video_too_large")) {
      const m = err.match(/exceeds\s+(\d+)\s*GB/i);
      return m ? `Skipped · over ${m[1]}GB` : "Skipped · file too large";
    }
    if (err) {
      const short = err.split(":")[0].replace(/_/g, " ");
      return `Skipped · ${short}`;
    }
    return "Skipped";
  }
  return "In library";
}
