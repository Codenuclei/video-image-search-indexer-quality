"use client";

import { ArrowRight } from "lucide-react";
import HeroVideoBg from "./HeroVideoBg";

const FEATURES = [
  { num: "01", label: "Themes & hooks", blurb: "Pull non-overlapping beats from the transcript" },
  { num: "02", label: "Frame select", blurb: "Pick stills that match each slide’s moment" },
  { num: "03", label: "Polish & export", blurb: "Edit copy, score quality, then ship the carousel" },
] as const;

export default function Hero() {
  return (
    <section className="relative flex min-h-[100svh] flex-col items-center overflow-x-hidden">
      <div className="pointer-events-none absolute inset-0 z-0 overflow-hidden">
        <HeroVideoBg />
      </div>

      <div className="relative z-10 flex w-full min-h-[100svh] flex-col items-center">
        <div className="flex flex-col items-center px-4 pt-24 text-center sm:px-6 sm:pt-28 md:pt-32">
          <h1 className="font-serif text-4xl font-normal leading-[1.1] tracking-tighter text-[#191919] sm:text-5xl md:text-7xl lg:text-8xl">
            Carousels from
            <br />
            your videos.
          </h1>
          <p className="mt-5 max-w-sm text-sm leading-relaxed text-[#191919]/70 sm:mt-6 sm:max-w-md md:mt-8 md:text-base">
            Turn indexed videos and transcripts into Instagram-ready carousels —
            pick themes, hooks, and frames in one studio.
          </p>
          <div className="mt-6 flex flex-wrap items-center justify-center gap-3 sm:mt-8 md:mt-10">
            <a
              href="/carousel"
              className="rounded-lg bg-[#191919] px-6 py-3 text-sm font-medium text-white transition-colors hover:bg-[#191919]/90 sm:px-8 sm:py-3.5"
            >
              Start creating
            </a>
            <a
              href="/library"
              className="rounded-lg border border-[#191919]/20 bg-white/80 px-6 py-3 text-sm font-medium text-[#191919] backdrop-blur-sm transition-colors hover:bg-white sm:px-8 sm:py-3.5"
            >
              Library
            </a>
          </div>
        </div>

        <div className="mt-auto w-full max-w-5xl px-4 pb-10 pt-10 sm:px-6 sm:pb-14 sm:pt-14">
          <div className="overflow-hidden rounded-t-2xl border border-gray-200 bg-white/95 px-5 pb-5 pt-8 shadow-sm backdrop-blur-sm sm:px-8 sm:pb-8 sm:pt-12 md:px-12 md:pt-16">
            <div className="grid gap-6 md:grid-cols-2 md:gap-16">
              <div>
                <p className="text-[11px] font-medium uppercase tracking-[0.2em] text-[#191919]/50">
                  What we do
                </p>
                <h2 className="mt-3 font-serif text-2xl font-normal leading-tight tracking-tight text-[#191919] sm:text-3xl md:text-4xl">
                  From transcript{" "}
                  <br className="hidden sm:block" />
                  to carousel
                </h2>
              </div>
              <div className="flex items-end">
                <p className="text-sm leading-relaxed text-[#191919]/70 md:text-[15px]">
                  Select a captioned video, surface themes and hooks, choose frames,
                  and generate slide layouts — with cached previews ready to polish.
                </p>
              </div>
            </div>

            <div className="mt-6 h-px w-full bg-gray-200 sm:mt-8 md:mt-10" />

            <div className="mt-4 grid gap-3 sm:grid-cols-3 sm:gap-4">
              {FEATURES.map((item) => (
                <a
                  key={item.num}
                  href="/carousel"
                  className="group flex flex-col gap-2 rounded-xl bg-[#F4F3F3] px-4 py-4 text-left transition-all duration-200 hover:bg-[#eaeaea] sm:px-5 sm:py-5"
                >
                  <span className="flex items-center justify-between gap-2 text-sm text-[#191919]">
                    <span>
                      <span className="text-[#191919]/40">{item.num}</span>
                      <span className="mx-2 text-[#191919]/30">/</span>
                      <span className="font-medium">{item.label}</span>
                    </span>
                    <ArrowRight className="h-4 w-4 shrink-0 text-gray-400 transition-all duration-200 group-hover:translate-x-0.5 group-hover:text-gray-700" />
                  </span>
                  <span className="text-xs leading-relaxed text-[#191919]/55">{item.blurb}</span>
                </a>
              ))}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
