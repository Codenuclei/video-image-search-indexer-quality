/**
 * Drive `/drive/sync` is fire-and-forget on the backend, so a list fetched
 * right after a folder switch can be stale. Refresh immediately, then re-poll
 * a few times so newly synced / captioned videos appear without a manual reload.
 * Returns a cancel function (call it before starting another round or on unmount).
 */
export function refreshAfterSync(
  load: () => Promise<unknown> | void,
  delaysMs: number[] = [0, 4_000, 10_000, 20_000]
): () => void {
  let cancelled = false;
  const timers: ReturnType<typeof setTimeout>[] = [];
  for (const delay of delaysMs) {
    timers.push(
      setTimeout(() => {
        if (!cancelled) void load();
      }, delay)
    );
  }
  return () => {
    cancelled = true;
    for (const t of timers) clearTimeout(t);
  };
}

export type ActiveDriveFolder = { id: string; name: string } | null;
