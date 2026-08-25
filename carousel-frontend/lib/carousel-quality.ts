import type { CarouselQualityReport } from "./api";

export type QualityUiStatus = "current" | "stale" | "rechecking";

export function qualityUiStatus(opts: {
  report?: CarouselQualityReport | null;
  stale: boolean;
  rechecking: boolean;
}): QualityUiStatus {
  if (opts.rechecking) return "rechecking";
  if (opts.stale || !opts.report) return "stale";
  return "current";
}

export function shouldHideQualityScore(status: QualityUiStatus): boolean {
  return status !== "current";
}

export const QUALITY_RECHECK_DEBOUNCE_MS = 700;
