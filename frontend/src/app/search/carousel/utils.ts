import type { CarouselSnapshotContext, SearchMoment } from "@/lib/api";

export function formatTimestamp(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function formatTimestampRange(start: number, end?: number | null): string {
  const startLabel = formatTimestamp(start);
  if (end != null && end > start + 0.5) {
    return `${startLabel}–${formatTimestamp(end)}`;
  }
  return startLabel;
}

export function momentToSnapshot(moment: SearchMoment): CarouselSnapshotContext {
  return {
    drive_file_id: moment.drive_file_id,
    name: moment.name,
    timestamp_sec: moment.timestamp_sec,
    end_timestamp_sec: moment.end_timestamp_sec ?? null,
    snippet: moment.snippet ?? null,
    match_type: moment.match_type,
    preview_url: moment.preview_url,
  };
}

export function momentKey(m: { drive_file_id: string; timestamp_sec: number }): string {
  return `${m.drive_file_id}:${m.timestamp_sec}`;
}

export function snapshotKey(s: CarouselSnapshotContext | null): string | null {
  if (!s) return null;
  return momentKey(s);
}

export function toggleId(list: string[], id: string): string[] {
  return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
}

export function mergePresets<T extends { id: string }>(prev: T[], extra: T[]): T[] {
  const seen = new Set(prev.map((p) => p.id));
  const merged = [...prev];
  for (const item of extra) {
    if (!seen.has(item.id)) {
      seen.add(item.id);
      merged.push(item);
    }
  }
  return merged;
}

export function slugify(label: string): string {
  return (
    label
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_|_$/g, "")
      .slice(0, 48) || "item"
  );
}

export function transcriptThemeId(title: string): string {
  return `tx:${slugify(title)}`;
}
