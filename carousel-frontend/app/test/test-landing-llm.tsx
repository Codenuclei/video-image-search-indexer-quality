"use client";

import Link from "next/link";
import { useState } from "react";
import {
  CarouselLlmPicker,
  DEFAULT_CAROUSEL_RUN_CONFIG,
  persistRunConfig,
} from "./carousel-llm-picker";
import type { CarouselRunConfig } from "@/lib/test-api";

/** Thin client island so the landing page can stay mostly static. */
export function TestLandingLlmControl() {
  const [runConfig, setRunConfig] = useState<CarouselRunConfig>(
    DEFAULT_CAROUSEL_RUN_CONFIG
  );
  const [note, setNote] = useState<string | null>(null);
  return (
    <div className="mt-8 flex w-full max-w-lg flex-col items-center text-left">
      <p className="mb-2 text-center text-[11px] font-medium uppercase tracking-[0.14em] text-slate-500">
        Carousel LLM
      </p>
      <CarouselLlmPicker
        compact
        showGenerate
        value={runConfig}
        onChange={(next) => {
          persistRunConfig(next);
          setRunConfig(next);
          setNote(null);
        }}
        onGenerate={(cfg) => {
          persistRunConfig(cfg);
          setNote(`Ready: ${cfg.provider} · ${cfg.model} — open Studio to run.`);
        }}
      />
      {note ? (
        <p className="mt-2 text-center text-xs text-slate-600" role="status">
          {note}
        </p>
      ) : null}
      <p className="mt-2 text-center text-xs text-slate-400">
        Applies to{" "}
        <Link href="/test/studio" className="underline underline-offset-2 hover:text-slate-600">
          /test/studio
        </Link>
        . You can change provider and model anytime — the next generate uses the current picker.
      </p>
    </div>
  );
}
