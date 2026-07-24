"use client";

import { FolderOpen, Youtube } from "lucide-react";

const FOLDERS = [
  { name: "Leadership Archive 2025", files: 412, status: "synced", wash: "sky" as const },
  { name: "Campus Media Drop", files: 187, status: "indexing", wash: "yellow" as const },
  { name: "YouTube imports", files: 54, status: "idle", wash: "orange" as const },
];

const WASH: Record<(typeof FOLDERS)[number]["wash"], string> = {
  sky: "bg-[var(--mu-sky-wash)]",
  yellow: "bg-[var(--mu-yellow-wash)]",
  orange: "bg-[var(--mu-orange-wash)]",
};

export function FoldersSummaryDemo() {
  return (
    <section className="demo-rise demo-rise-d3">
      <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="demo-eyebrow">Folders</p>
          <h3 className="demo-section-title">Source map</h3>
        </div>
        <p className="text-xs font-medium text-[var(--mu-n600)]">3 linked · 653 media files</p>
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        {FOLDERS.map((f) => (
          <article
            key={f.name}
            className={`rounded-[12px] border border-[var(--mu-n200)] p-5 ${WASH[f.wash]}`}
          >
            <div className="mb-4 flex items-center justify-between">
              {f.name.includes("YouTube") ? (
                <Youtube size={16} className="text-[var(--mu-orange)]" />
              ) : (
                <FolderOpen
                  size={16}
                  className={f.wash === "sky" ? "text-[var(--mu-sky)]" : "text-[var(--mu-n950)]"}
                />
              )}
              <span
                className={`rounded-[999px] px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${
                  f.status === "synced"
                    ? "bg-[var(--mu-success-wash)] text-[var(--mu-success)]"
                    : f.status === "indexing"
                      ? "bg-white text-[var(--mu-n950)]"
                      : "bg-white text-[var(--mu-n500)]"
                }`}
              >
                {f.status}
              </span>
            </div>
            <h4 className="line-clamp-2 text-sm font-semibold leading-snug">{f.name}</h4>
            <p className="mt-3 text-3xl tabular-nums font-bold tracking-tight">
              {f.files}
              <span className="ml-1.5 text-xs font-medium tracking-normal text-[var(--mu-n600)]">
                files
              </span>
            </p>
          </article>
        ))}
      </div>
    </section>
  );
}
