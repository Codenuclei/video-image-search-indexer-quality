"use client";

import { apiAssetUrl, type CarouselCueItem, type CarouselSnapshotContext, type SearchMoment } from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatTimestampRange, momentKey, momentToSnapshot, snapshotKey } from "./utils";

export function SnapshotCuesPanel({
  cues,
  loading,
  moments,
  activeSnapshot,
  onPickCue,
  onUseMoment,
}: {
  cues: CarouselCueItem[];
  loading?: boolean;
  moments: SearchMoment[];
  activeSnapshot: CarouselSnapshotContext | null;
  onPickCue: (cue: CarouselCueItem) => void;
  onUseMoment?: (moment: SearchMoment) => void;
}) {
  const activeKey = snapshotKey(activeSnapshot);

  if (loading) {
    return <p className="text-sm font-medium text-muted-foreground">Matching spoken cues…</p>;
  }

  if (!cues.length) {
    return (
      <p className="text-sm font-medium text-muted-foreground">
        Select hooks or topics to surface spoken snapshot cues.
      </p>
    );
  }

  return (
    <ul className="divide-y divide-border border-y border-border">
      {cues.map((cue) => {
        const snap = cue.snapshot ?? null;
        const key = snapshotKey(snap);
        const isActive = key != null && key === activeKey;
        const altMoments = moments.filter((m) => !snap || momentKey(m) !== key).slice(0, 4);

        return (
          <li
            key={`${cue.kind}-${cue.id}`}
            className={cn(
              "flex flex-col gap-3 py-3 sm:flex-row sm:items-start",
              isActive && "bg-muted -mx-3 px-3 sm:-mx-4 sm:px-4"
            )}
          >
            <div className="flex min-w-0 flex-1 gap-3">
              {snap?.preview_url && (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={apiAssetUrl(snap.preview_url)}
                  alt=""
                  className="h-14 w-24 shrink-0 rounded-[4px] object-cover bg-muted"
                />
              )}
              <div className="min-w-0">
                <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                  {cue.kind}
                </p>
                <p className="mt-0.5 text-sm font-semibold text-foreground">{cue.label}</p>
                {(cue.cue_text || snap?.snippet) && (
                  <p className="mt-0.5 line-clamp-2 text-xs font-medium text-muted-foreground">
                    “{cue.cue_text || snap?.snippet}”
                  </p>
                )}
                {snap && (
                  <p className="mt-1 text-[11px] font-medium text-muted-foreground">
                    {formatTimestampRange(snap.timestamp_sec, snap.end_timestamp_sec)}
                    {cue.score != null ? ` · ${cue.score}` : ""}
                  </p>
                )}
              </div>
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-2">
              {snap && (
                <button
                  type="button"
                  className={cn(
                    "studio-btn studio-btn-sm",
                    isActive ? "studio-btn-accent" : "studio-btn-secondary"
                  )}
                  onClick={() => onPickCue(cue)}
                >
                  {isActive ? "Active cue" : "Use cue"}
                </button>
              )}
              {onUseMoment &&
                altMoments.map((m) => (
                  <button
                    key={momentKey(m)}
                    type="button"
                    className="studio-btn studio-btn-ghost"
                    onClick={() => onUseMoment(m)}
                    title={m.snippet || m.name}
                  >
                    {formatTimestampRange(m.timestamp_sec, m.end_timestamp_sec)}
                  </button>
                ))}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

export function cueToSnapshot(cue: CarouselCueItem): CarouselSnapshotContext | null {
  if (cue.snapshot) return cue.snapshot;
  return null;
}

export { momentToSnapshot };
