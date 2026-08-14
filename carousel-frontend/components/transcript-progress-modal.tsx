"use client";

import { Loader2, X } from "lucide-react";
import { ModalOverlay } from "@/components/modal";

export type TranscriptModalState = {
  open: boolean;
  videoName?: string;
  message?: string;
  phase?: string | null;
  error?: string | null;
  cueCount?: number | null;
};

function modalTitle(state: TranscriptModalState, failed: boolean, ready: boolean): string {
  if (failed) return "Couldn’t prepare transcript";
  if (ready) return "English transcript ready";
  const phase = (state.phase || "").toLowerCase();
  if (phase.includes("english")) return "Preparing English transcript";
  return "Getting transcripts from the video";
}

export function TranscriptProgressModal({
  state,
  onClose,
  onRetry,
}: {
  state: TranscriptModalState;
  onClose: () => void;
  onRetry?: () => void;
}) {
  const failed = Boolean(state.error);
  const ready = !failed && typeof state.cueCount === "number" && state.cueCount > 0;

  return (
    <ModalOverlay open={state.open} onClose={failed || ready ? onClose : () => {}}>
      <div
        className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl"
        role="dialog"
        aria-modal="true"
        aria-labelledby="transcript-modal-title"
        data-testid="transcript-progress-modal"
      >
        <div className="flex items-start justify-between gap-3 border-b border-slate-100 px-5 py-4">
          <div className="min-w-0">
            <h2
              id="transcript-modal-title"
              className="text-base font-semibold text-slate-900"
            >
              {modalTitle(state, failed, ready)}
            </h2>
            {state.videoName ? (
              <p className="mt-1 truncate text-xs text-slate-500">{state.videoName}</p>
            ) : null}
          </div>
          {(failed || ready) && (
            <button
              type="button"
              className="rounded-lg p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-800"
              onClick={onClose}
              aria-label="Close"
            >
              <X size={16} />
            </button>
          )}
        </div>

        <div className="px-5 py-8 text-center">
          {!failed && !ready ? (
            <>
              <Loader2
                size={28}
                className="mx-auto animate-spin text-slate-700"
                aria-hidden
              />
              <p className="mt-4 text-sm font-medium text-slate-800">
                {state.message || "Getting transcripts from the video…"}
              </p>
              <p className="mt-2 text-xs leading-relaxed text-slate-500">
                {(state.phase || "").toLowerCase().includes("english")
                  ? "We’re turning the transcript into complete English sentences that match the video timing. This usually takes a moment."
                  : "Speech recognition runs on the indexed video file. This can take a few minutes for longer videos — keep this tab open."}
              </p>
            </>
          ) : failed ? (
            <>
              <p className="text-sm text-red-600" role="alert">
                {state.error ||
                  "We couldn’t prepare this transcript. Nothing unsafe was saved — please try again."}
              </p>
              <div className="mt-5 flex flex-wrap items-center justify-center gap-2">
                {onRetry ? (
                  <button
                    type="button"
                    className="studio-btn studio-btn-primary"
                    onClick={onRetry}
                  >
                    Try again
                  </button>
                ) : null}
                <button type="button" className="studio-btn studio-btn-ghost" onClick={onClose}>
                  Close
                </button>
              </div>
            </>
          ) : (
            <>
              <p className="text-sm text-emerald-700">
                {state.message ||
                  `English transcript ready (${state.cueCount ?? 0} sentences). You can continue.`}
              </p>
              <button
                type="button"
                className="studio-btn studio-btn-primary mt-5"
                onClick={onClose}
              >
                Continue
              </button>
            </>
          )}
        </div>
      </div>
    </ModalOverlay>
  );
}
