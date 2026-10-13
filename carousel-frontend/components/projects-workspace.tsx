"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Clapperboard, RefreshCw } from "lucide-react";
import { FrameImg } from "@/components/frame-img";
import { LoadingLabel } from "@/components/ui";
import type { CarouselProjectCard } from "@/lib/carousel-draft";
import { formatApiError } from "@/lib/api";

const PROJECTS_PAGE = 24;

type ProjectsWorkspaceProps = {
  studioBasePath: "/carousel" | "/test/studio";
  listProjects: (opts: {
    limit: number;
    offset: number;
    q?: string;
  }) => Promise<{ items: CarouselProjectCard[]; has_more: boolean }>;
  deleteDraft: (driveFileId: string) => Promise<unknown>;
  assetUrl: (path: string) => string;
  /** Optional search query controlled by parent; empty = no filter. */
  query?: string;
  /** Bump to force a reload (e.g. parent Refresh). */
  refreshToken?: number;
};

function safePreview(url: string | null | undefined, assetUrl: (p: string) => string): string | null {
  if (!url || typeof url !== "string") return null;
  const trimmed = url.trim();
  if (!trimmed.startsWith("/media/") && !trimmed.startsWith("/search/")) return null;
  return assetUrl(trimmed);
}

function formatWhen(iso: string | null | undefined): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function projectChip(card: CarouselProjectCard): "Draft" | "Finished" | "Both" {
  if (card.has_draft && card.finished_count > 0) return "Both";
  if (card.has_draft) return "Draft";
  return "Finished";
}

export function ProjectsWorkspace({
  studioBasePath,
  listProjects,
  deleteDraft,
  assetUrl,
  query = "",
  refreshToken = 0,
}: ProjectsWorkspaceProps) {
  const [items, setItems] = useState<CarouselProjectCard[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [offset, setOffset] = useState(0);
  const [discarding, setDiscarding] = useState<string | null>(null);

  const load = useCallback(
    async (opts?: { append?: boolean; offset?: number }) => {
      const append = Boolean(opts?.append);
      const nextOffset = opts?.offset ?? 0;
      if (append) setLoadingMore(true);
      else {
        setLoading(true);
        setError(null);
      }
      try {
        const res = await listProjects({
          limit: PROJECTS_PAGE,
          offset: nextOffset,
          q: query.trim() || undefined,
        });
        const page = res.items ?? [];
        setItems((prev) => (append ? [...prev, ...page] : page));
        setHasMore(Boolean(res.has_more));
        setOffset(nextOffset + page.length);
      } catch (e) {
        if (!append) {
          setItems([]);
          setHasMore(false);
          setOffset(0);
        }
        setError(formatApiError(e, "Could not load projects"));
      } finally {
        setLoading(false);
        setLoadingMore(false);
      }
    },
    [listProjects, query]
  );

  useEffect(() => {
    void load();
  }, [load, refreshToken]);

  async function discardDraft(driveFileId: string) {
    if (!driveFileId || discarding) return;
    setDiscarding(driveFileId);
    try {
      await deleteDraft(driveFileId);
      setItems((prev) =>
        prev
          .map((card) => {
            if (card.drive_file_id !== driveFileId) return card;
            if (card.finished_count > 0) {
              return {
                ...card,
                has_draft: false,
                draft_id: null,
                draft_phase: null,
                draft_updated_at: null,
              };
            }
            return null;
          })
          .filter((c): c is CarouselProjectCard => c != null)
      );
    } catch (e) {
      setError(formatApiError(e, "Could not discard draft"));
    } finally {
      setDiscarding(null);
    }
  }

  return (
    <section className="mb-10" aria-labelledby="projects-workspace-heading">
      <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2
            id="projects-workspace-heading"
            className="text-sm font-semibold text-slate-800"
          >
            Projects
          </h2>
          <p className="mt-0.5 text-xs text-slate-500">
            Per-video workspace — resume drafts or open the latest finished carousel.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-60"
        >
          <RefreshCw size={12} className={loading ? "animate-spin" : undefined} />
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {loading && items.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-2 rounded-xl bg-slate-50 px-4 py-8 text-center">
          <p className="text-sm text-slate-500">
            <LoadingLabel>Loading projects…</LoadingLabel>
          </p>
        </div>
      ) : error && items.length === 0 ? (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-6 text-center">
          <p className="text-sm text-red-800">{error}</p>
          <button
            type="button"
            className="mt-3 rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800"
            onClick={() => void load()}
          >
            Try again
          </button>
        </div>
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-2 rounded-xl bg-slate-50 px-4 py-8 text-center">
          <Clapperboard size={20} className="text-slate-400" />
          <p className="text-sm text-slate-500">
            No drafts or finished carousels yet. Start from a video below.
          </p>
          <Link
            href={studioBasePath}
            className="mt-1 text-xs font-medium text-slate-700 underline-offset-2 hover:underline"
          >
            Open studio
          </Link>
        </div>
      ) : (
        <>
          <ul className="grid gap-3 sm:grid-cols-2">
            {items.map((card) => {
              const chip = projectChip(card);
              const thumb = safePreview(card.latest_finished?.preview_url, assetUrl);
              const title =
                card.video_name?.trim() ||
                card.latest_finished?.label?.trim() ||
                card.drive_file_id;
              const resumeHref = `${studioBasePath}?video=${encodeURIComponent(card.drive_file_id)}&draft=1`;
              const finishedHref = card.latest_finished
                ? `${studioBasePath}?video=${encodeURIComponent(card.drive_file_id)}&save=${card.latest_finished.id}`
                : null;
              return (
                <li
                  key={card.drive_file_id}
                  className="flex gap-3 overflow-hidden rounded-xl border border-slate-200 bg-white/90 p-3"
                  data-testid="project-card"
                >
                  <div className="relative h-20 w-14 shrink-0 overflow-hidden rounded-md bg-slate-100">
                    {thumb ? (
                      <FrameImg src={thumb} alt="" className="h-full w-full object-cover" />
                    ) : (
                      <div className="flex h-full w-full items-center justify-center text-slate-400">
                        <Clapperboard size={18} />
                      </div>
                    )}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="truncate text-sm font-medium text-slate-900">{title}</p>
                      <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-slate-600">
                        {chip}
                      </span>
                    </div>
                    <p className="mt-0.5 truncate text-xs text-slate-500">
                      {card.drive_file_id}
                    </p>
                    <p className="mt-1 text-[11px] text-slate-500">
                      {card.has_draft && card.draft_phase != null
                        ? `Step ${card.draft_phase}`
                        : null}
                      {card.has_draft && card.draft_phase != null && card.last_activity_at
                        ? " · "
                        : null}
                      {card.last_activity_at
                        ? `Last edited ${formatWhen(card.last_activity_at)}`
                        : null}
                      {card.finished_count > 0
                        ? ` · ${card.finished_count} finished`
                        : null}
                    </p>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {card.has_draft ? (
                        <Link
                          href={resumeHref}
                          className="rounded-md bg-slate-900 px-2.5 py-1.5 text-xs font-medium text-white hover:bg-slate-800"
                        >
                          Resume draft
                        </Link>
                      ) : null}
                      {finishedHref ? (
                        <Link
                          href={finishedHref}
                          className="rounded-md border border-slate-200 px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
                        >
                          Open latest finished
                        </Link>
                      ) : null}
                      {card.has_draft ? (
                        <button
                          type="button"
                          disabled={discarding === card.drive_file_id}
                          onClick={() => void discardDraft(card.drive_file_id)}
                          className="rounded-md border border-slate-200 px-2.5 py-1.5 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-60"
                        >
                          {discarding === card.drive_file_id ? "Discarding…" : "Discard draft"}
                        </button>
                      ) : null}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
          {error ? (
            <p className="mt-2 text-xs text-red-700" role="alert">
              {error}
            </p>
          ) : null}
          {hasMore ? (
            <div className="mt-4 flex justify-center">
              <button
                type="button"
                disabled={loadingMore}
                onClick={() => void load({ append: true, offset })}
                className="rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-60"
              >
                {loadingMore ? <LoadingLabel>Loading more…</LoadingLabel> : "Load more"}
              </button>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
