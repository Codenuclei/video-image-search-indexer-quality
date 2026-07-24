"use client";

import { GalleryHorizontal, Play, Search, Sparkles } from "lucide-react";

const SLIDES = [
  { t: "0:42", title: "Opening keynote — room energy", tag: "Hook", wash: "yellow" as const },
  { t: "3:18", title: "Board Q&A · decision framing", tag: "Claim", wash: "sky" as const },
  { t: "7:05", title: "Alumni story · mid-scene cut", tag: "Story", wash: "orange" as const },
  { t: "11:22", title: "Closing challenge to the room", tag: "CTA", wash: "yellow" as const },
];

const WASH: Record<(typeof SLIDES)[number]["wash"], string> = {
  yellow: "bg-[var(--mu-yellow-wash)]",
  sky: "bg-[var(--mu-sky-wash)]",
  orange: "bg-[var(--mu-orange-wash)]",
};

export function SearchCarouselHeroDemo() {
  return (
    <section className="demo-rise demo-rise-d4 demo-card overflow-hidden">
      <div className="border-b border-[var(--mu-n200)] pb-6">
        <p className="demo-eyebrow">Search · Video carousel</p>
        <h3 className="demo-section-title max-w-xl">
          Find the moment. Script the carousel.
        </h3>
        <p className="mt-2 max-w-lg text-sm font-medium text-[var(--mu-n600)]">
          Semantic video search meets outline generation — mock of the Find → Carousel surface.
        </p>

        <div className="mt-5 flex flex-col gap-3 sm:flex-row sm:items-center">
          <div className="demo-input flex flex-1 items-center gap-2 px-4 py-2.5">
            <Search size={15} className="shrink-0 text-[var(--mu-n500)]" />
            <span className="truncate text-sm text-[var(--mu-n500)]">
              “board deciding under pressure”
            </span>
          </div>
          <button type="button" className="demo-btn demo-btn-olive">
            <Sparkles size={14} />
            Outline
          </button>
        </div>
      </div>

      <div className="mt-6 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-[var(--mu-n500)]">
        <GalleryHorizontal size={14} />
        Snapshots
      </div>
      <div className="mt-3 flex gap-3 overflow-x-auto pb-1">
        {SLIDES.map((s) => (
          <article
            key={s.t}
            className="group relative w-[min(220px,70vw)] shrink-0 overflow-hidden rounded-[12px] border border-[var(--mu-n200)] bg-white"
          >
            <div className={`relative aspect-video ${WASH[s.wash]}`}>
              <div className="absolute left-3 top-3 rounded-[999px] bg-white px-2 py-0.5 text-[10px] font-bold tabular-nums text-[var(--mu-n950)]">
                {s.t}
              </div>
              <div className="absolute inset-0 flex items-center justify-center opacity-0 transition group-hover:opacity-100">
                <span className="flex h-10 w-10 items-center justify-center rounded-full bg-[var(--mu-yellow)] text-[var(--mu-n950)]">
                  <Play size={16} fill="currentColor" />
                </span>
              </div>
            </div>
            <div className="p-3">
              <p className="text-[10px] font-bold uppercase tracking-wider text-[var(--mu-sky)]">
                {s.tag}
              </p>
              <p className="mt-1 line-clamp-2 text-xs font-semibold leading-snug">{s.title}</p>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
