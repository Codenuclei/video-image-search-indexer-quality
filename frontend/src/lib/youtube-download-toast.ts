import { toast } from "sonner";
import { apiClient, type DriveFile } from "@/lib/api";

function formatBytes(n: number | null | undefined): string {
  if (n == null || n <= 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

export type TrackedYt = {
  driveFileId: string;
  name: string;
};

function summaryLine(items: { status: string; size: number | null }[]): string {
  const by = { processing: 0, pending: 0, processed: 0, error: 0, other: 0 };
  let totalSize = 0;
  for (const it of items) {
    if (it.status in by) by[it.status as keyof typeof by] += 1;
    else by.other += 1;
    if (it.size) totalSize += it.size;
  }
  const parts: string[] = [];
  if (by.processing) parts.push(`${by.processing} downloading`);
  if (by.pending) parts.push(`${by.pending} queued`);
  if (by.processed) parts.push(`${by.processed} ready`);
  if (by.error) parts.push(`${by.error} failed`);
  if (by.other) parts.push(`${by.other} other`);
  if (totalSize > 0) parts.push(formatBytes(totalSize));
  return parts.join(" · ") || "Waiting…";
}

function pickTracked(files: DriveFile[], ids: Set<string>): DriveFile[] {
  return files.filter((f) => ids.has(f.id));
}

/** Bottom Sonner toast that polls YouTube download / index status until settled. */
export function trackYoutubeDownloads(
  tracked: TrackedYt[],
  opts?: { toastId?: string | number },
): void {
  if (!tracked.length) return;
  const toastId = opts?.toastId ?? `yt-dl-${Date.now()}`;
  const ids = new Set(tracked.map((t) => t.driveFileId));
  const title =
    tracked.length === 1
      ? tracked[0].name || "YouTube download"
      : `YouTube · ${tracked.length} videos`;

  toast.loading(title, {
    id: toastId,
    description: "Queued — fetching status…",
    duration: Infinity,
  });

  let ticks = 0;
  const maxTicks = 180; // ~15 min at 5s

  const tick = async () => {
    ticks += 1;
    try {
      const files = await apiClient.youtubeVideos();
      const rows = pickTracked(files, ids);
      const items =
        rows.length > 0
          ? rows.map((f) => ({ status: f.status, size: f.size }))
          : tracked.map(() => ({ status: "processing", size: null as number | null }));

      const desc = summaryLine(items);
      const allDone =
        rows.length >= tracked.length &&
        rows.every((f) => f.status === "processed" || f.status === "error");

      if (allDone) {
        const failedRows = rows.filter((f) => f.status === "error");
        const failed = failedRows.length;
        const ok = rows.filter((f) => f.status === "processed").length;
        const errHint = failedRows
          .map((f) => f.error_message)
          .find((m): m is string => !!m && m.trim().length > 0);
        const failDesc = errHint
          ? `${desc} — ${errHint.length > 220 ? `${errHint.slice(0, 217)}…` : errHint}`
          : desc;
        if (failed && !ok) {
          toast.error(title, { id: toastId, description: failDesc, duration: 16_000 });
        } else if (failed) {
          toast.warning(title, { id: toastId, description: failDesc, duration: 12_000 });
        } else {
          toast.success(title, { id: toastId, description: desc, duration: 8_000 });
        }
        return;
      }

      toast.loading(title, {
        id: toastId,
        description: desc,
        duration: Infinity,
      });
    } catch {
      toast.loading(title, {
        id: toastId,
        description: "Still working… (status refresh failed)",
        duration: Infinity,
      });
    }

    if (ticks < maxTicks) {
      setTimeout(() => void tick(), 5000);
    } else {
      toast.message(title, {
        id: toastId,
        description: "Still running in background — check Folders for status.",
        duration: 10_000,
      });
    }
  };

  void tick();
  setTimeout(() => void tick(), 2500);
}
