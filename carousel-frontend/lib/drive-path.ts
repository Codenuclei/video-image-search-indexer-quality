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

/** Human-readable status line, including skip reasons when present.
 *
 * Keep this short — never echo the file path/name (that was the #11
 * "extra space" / clutter bug in the Drive select modal).
 */
export function driveFileStatusLabel(
  status: string | null | undefined,
  errorMessage?: string | null
): string {
  const key = (status || "").trim().toLowerCase();
  const err = (errorMessage || "").trim();
  if (key === "pending") return "Waiting";
  if (key === "processing") return "Indexing…";
  if (key === "processed") return "Ready";
  if (key === "error") {
    // Prefer a short machine reason (before ':') — never a Drive path.
    if (err && !err.includes("/") && !err.includes("\\")) {
      const short = err.split(":")[0].replace(/_/g, " ").trim();
      if (short && short.length <= 28 && short.toLowerCase() !== key) {
        return `Failed · ${short}`;
      }
    }
    return "Failed";
  }
  if (key === "skipped") {
    if (err.toLowerCase().startsWith("video_too_large")) {
      const m = err.match(/exceeds\s+(\d+)\s*GB/i);
      return m ? `Skipped · over ${m[1]}GB` : "Skipped · too large";
    }
    if (err && !err.includes("/") && !err.includes("\\")) {
      const short = err.split(":")[0].replace(/_/g, " ").trim();
      if (short && short.length <= 28) return `Skipped · ${short}`;
    }
    return "Skipped";
  }
  return "In library";
}
