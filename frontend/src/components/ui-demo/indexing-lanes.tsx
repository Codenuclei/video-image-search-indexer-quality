"use client";

import type { ReactNode } from "react";
import { Film, Image as ImageIcon } from "lucide-react";

const IMAGE_FILES = ["retreat_group_03.jpg", "keynote_stage.png", "alumni_dinner.webp"];
const VIDEO_FILES = ["panel_qna_cut.mp4", "campus_walkthrough.mov"];

export function IndexingLanesDemo() {
  return (
    <section className="demo-rise demo-rise-d2 demo-card">
      <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="demo-eyebrow">Indexing · dual lane</p>
          <h3 className="demo-section-title">Live pipeline status</h3>
        </div>
        <div className="flex items-center gap-2 text-xs font-medium text-[var(--mu-n600)]">
          <span className="inline-flex h-2 w-2 rounded-full bg-[var(--mu-sky)]" />
          Running · 1,248 done · 86 pending
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Lane
          icon={<ImageIcon size={16} />}
          label="Images"
          active={2}
          max={4}
          tone="sky"
          files={IMAGE_FILES}
          progress={68}
        />
        <Lane
          icon={<Film size={16} />}
          label="Videos"
          active={1}
          max={2}
          tone="orange"
          files={VIDEO_FILES}
          progress={41}
          delay
        />
      </div>
    </section>
  );
}

function Lane({
  icon,
  label,
  active,
  max,
  tone,
  files,
  progress,
  delay,
}: {
  icon: ReactNode;
  label: string;
  active: number;
  max: number;
  tone: "sky" | "orange";
  files: string[];
  progress: number;
  delay?: boolean;
}) {
  const isSky = tone === "sky";

  return (
    <div
      className={`rounded-[12px] border border-[var(--mu-n200)] p-4 ${
        isSky ? "bg-[var(--mu-sky-wash)]" : "bg-[var(--mu-orange-wash)]"
      }`}
    >
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-semibold">
          {icon}
          {label}
        </div>
        <span className="text-sm tabular-nums font-bold">
          {active}/{max}
        </span>
      </div>
      <div className="mb-3 h-1.5 overflow-hidden rounded-full bg-white">
        <div
          className={`demo-lane-bar h-full rounded-full ${delay ? "demo-lane-bar-delay" : ""} ${
            isSky ? "bg-[var(--mu-sky)]" : "bg-[var(--mu-orange)]"
          }`}
          style={{ width: `${progress}%` }}
        />
      </div>
      <ul className="space-y-1.5">
        {files.map((f) => (
          <li key={f} className="truncate text-[11px] font-medium text-[var(--mu-n600)]">
            <span
              className={`mr-1.5 inline-block h-1.5 w-1.5 rounded-full ${
                isSky ? "bg-[var(--mu-sky)]" : "bg-[var(--mu-orange)]"
              }`}
            />
            {f}
          </li>
        ))}
      </ul>
    </div>
  );
}
