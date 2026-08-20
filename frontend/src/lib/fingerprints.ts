/** Build/compare lightweight revision strings for conditional refresh. */

export function indexStatusRevision(input: {
  last_run_at?: string | null;
  is_running?: boolean;
  pending_count?: number;
  counts_by_status?: Record<string, number>;
  revision?: string | null;
}): string {
  if (input.revision) return input.revision;
  const counts = input.counts_by_status ?? {};
  const countsPart = Object.keys(counts)
    .sort()
    .map((k) => `${k}:${counts[k]}`)
    .join(",");
  return [
    input.last_run_at ?? "",
    input.is_running ? "1" : "0",
    String(input.pending_count ?? ""),
    countsPart,
  ].join("|");
}

export function cacheStatusRevision(input: {
  cached_at?: string | null;
  count?: number;
  file_count?: number;
}): string {
  return `${input.cached_at ?? ""}:${input.count ?? 0}:${input.file_count ?? 0}`;
}

export function personsRevision(
  persons: { id: number; occurrence_count?: number; name?: string }[],
  serverRevision?: string | null
): string {
  if (serverRevision) return serverRevision;
  const n = persons.length;
  let maxId = 0;
  let sumOcc = 0;
  for (const p of persons) {
    if (p.id > maxId) maxId = p.id;
    sumOcc += p.occurrence_count ?? 0;
  }
  return `${n}:${maxId}:${sumOcc}`;
}

export function stableJsonHash(value: unknown): string {
  try {
    const s = JSON.stringify(value);
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (Math.imul(31, h) + s.charCodeAt(i)) | 0;
    return String(h);
  } catch {
    return String(Date.now());
  }
}
