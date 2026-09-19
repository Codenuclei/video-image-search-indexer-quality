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
