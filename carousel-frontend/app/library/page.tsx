"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Clapperboard, Film, RefreshCw } from "lucide-react";
import {
  apiAssetUrl,
  apiClient,
  driveFileDownloadUrl,
  formatApiError,
  type CarouselGalleryItem,
  type CarouselRecentVideo,
  type YoutubeDriveFile,
} from "@/lib/api";
import { downloadFromUrl } from "@/lib/download";
import StudioLogo from "@/components/StudioLogo";
import { FrameImg } from "@/components/frame-img";
import { LoadingLabel } from "@/components/ui";

const GALLERY_PAGE = 24;

function safeGalleryPreview(url: string | null | undefined): string | null {
  if (!url || typeof url !== "string") return null;
  const trimmed = url.trim();
  if (!trimmed.startsWith("/media/") && !trimmed.startsWith("/search/")) return null;
  return apiAssetUrl(trimmed);
}

function formatWhen(iso: string | null | undefined): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function LibraryPage() {
  const [videos, setVideos] = useState<CarouselRecentVideo[]>([]);
  const [youtube, setYoutube] = useState<YoutubeDriveFile[]>([]);
  const [loading, setLoading] = useState(true);
  const [, setError] = useState<string | null>(null);

  const [saves, setSaves] = useState<CarouselGalleryItem[]>([]);
  const [savesLoading, setSavesLoading] = useState(true);
  const [savesLoadingMore, setSavesLoadingMore] = useState(false);
  const [savesError, setSavesError] = useState<string | null>(null);
  const [savesHasMore, setSavesHasMore] = useState(false);
  const [savesOffset, setSavesOffset] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [lib, yt] = await Promise.all([
        apiClient.carouselVideos({ limit: 60, captionedOnly: false }),
        apiClient.youtubeVideos().catch(() => [] as YoutubeDriveFile[]),
      ]);
      setVideos(lib.items ?? []);
      setYoutube(yt ?? []);
    } catch (e) {
      setError(formatApiError(e, "Could not load library"));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadSaves = useCallback(async (opts?: { append?: boolean; offset?: number }) => {
    const append = Boolean(opts?.append);
    const offset = opts?.offset ?? 0;
    if (append) setSavesLoadingMore(true);
    else {
      setSavesLoading(true);
      setSavesError(null);
    }
    try {
      const res = await apiClient.carouselGallery({ limit: GALLERY_PAGE, offset });
      const items = res.items ?? [];
      setSaves((prev) => (append ? [...prev, ...items] : items));
      setSavesHasMore(Boolean(res.has_more));
      setSavesOffset(offset + items.length);
    } catch (e) {
      if (!append) {
        setSaves([]);
        setSavesHasMore(false);
        setSavesOffset(0);
      }
      setSavesError(formatApiError(e, "Could not load saved carousels"));
    } finally {
      setSavesLoading(false);
      setSavesLoadingMore(false);
    }
  }, []);

  useEffect(() => {
    void load();
    void loadSaves();
  }, [load, loadSaves]);

  function refreshAll() {
    void load();
    void loadSaves();
  }

  return (
    <div className="relative min-h-screen overflow-x-hidden text-slate-900">
      <div className="absolute inset-0 -z-10 size-full bg-white [background:radial-gradient(125%_125%_at_50%_10%,#f8fafc_35%,#e2e8f0_55%,#1e293b_100%)]" />
      <nav className="sticky top-0 z-50 border-b border-slate-200 bg-white shadow-sm">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3.5 sm:px-6">
          <Link href="/" className="flex items-center gap-2.5 text-slate-900">
            <StudioLogo className="h-5 w-5" />
            <span className="text-sm font-semibold tracking-tight">Carousel Studio</span>
          </Link>
          <div className="flex items-center gap-3">
            <span
              className="hidden h-9 items-center rounded-lg border border-slate-900 bg-slate-900 px-4 text-sm font-medium text-white shadow-sm sm:inline-flex"
              aria-current="page"
            >
              Library
            </span>
            <Link
              href="/carousel"
              className="text-sm text-slate-500 transition-colors hover:text-slate-900"
            >
              Studio
            </Link>
          </div>
        </div>
      </nav>

      <main className="relative z-10 mx-auto max-w-5xl px-4 pb-12 pt-8 sm:px-6 sm:pt-10">
        <header className="mb-8">
          <p className="text-[11px] font-medium uppercase tracking-[0.2em] text-slate-500">
            Library
          </p>
          <h1 className="mt-2 font-serif text-3xl tracking-tight text-slate-900 sm:text-4xl">
            Captioned &amp; YouTube videos
          </h1>
          <p className="mt-2 max-w-xl text-sm text-slate-500">
            Browse indexed videos for carousel studio, reopen past ready generations, or open a
            YouTube download from the shared library.
          </p>
          <button
            type="button"
            onClick={() => refreshAll()}
            disabled={loading || savesLoading}
            className="mt-4 inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-60"
          >
            <RefreshCw
              size={14}
              className={loading || savesLoading ? "animate-spin" : undefined}
            />
            {loading || savesLoading ? <LoadingLabel>Refreshing…</LoadingLabel> : "Refresh"}
          </button>
        </header>

        <section className="mb-10" aria-labelledby="saved-carousels-heading">
          <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
            <div>
              <h2
                id="saved-carousels-heading"
                className="text-sm font-semibold text-slate-800"
              >
                Saved carousels
              </h2>
              <p className="mt-0.5 text-xs text-slate-500">
                Ready generations across all videos — open the exact save in studio.
              </p>
            </div>
            <button
              type="button"
              onClick={() => void loadSaves()}
              disabled={savesLoading}
              className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-60"
            >
              <RefreshCw size={12} className={savesLoading ? "animate-spin" : undefined} />
              {savesLoading ? "Loading…" : "Refresh gallery"}
            </button>
          </div>

          {savesLoading && saves.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-2 rounded-xl bg-slate-50 px-4 py-8 text-center">
              <p className="text-sm text-slate-500">
                <LoadingLabel>Loading saved carousels…</LoadingLabel>
              </p>
            </div>
          ) : savesError && saves.length === 0 ? (
            <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-6 text-center">
              <p className="text-sm text-red-800">{savesError}</p>
              <button
                type="button"
                className="mt-3 rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800"
                onClick={() => void loadSaves()}
              >
                Try again
              </button>
            </div>
          ) : saves.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-2 rounded-xl bg-slate-50 px-4 py-8 text-center">
              <Clapperboard size={20} className="text-slate-400" />
              <p className="text-sm text-slate-500">
                No ready carousel generations yet. Create one in studio, then it will show up here.
              </p>
              <Link
                href="/carousel"
                className="mt-1 text-xs font-medium text-slate-700 underline-offset-2 hover:underline"
              >
                Open studio
              </Link>
            </div>
          ) : (
            <>
              <ul className="grid gap-3 sm:grid-cols-2">
                {saves.map((item) => {
                  const thumb = safeGalleryPreview(item.preview_url);
                  const title =
                    item.label?.trim() ||
                    item.video_name?.trim() ||
                    `Carousel #${item.id}`;
                  const studioHref = `/carousel?video=${encodeURIComponent(item.drive_file_id)}&save=${item.id}`;
                  const testHref = `/test/studio?video=${encodeURIComponent(item.drive_file_id)}&save=${item.id}`;
                  return (
                    <li
                      key={item.id}
                      className="flex gap-3 overflow-hidden rounded-xl border border-slate-200 bg-white/90 p-3"
                    >
                      <div className="relative h-20 w-14 shrink-0 overflow-hidden rounded-md bg-slate-100">
                        {thumb ? (
                          <FrameImg
                            src={thumb}
                            alt=""
                            className="h-full w-full object-cover"
                          />
                        ) : (
                          <div className="flex h-full w-full items-center justify-center text-slate-400">
                            <Clapperboard size={18} />
                          </div>
                        )}
                      </div>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium text-slate-900">{title}</p>
                        <p className="mt-0.5 truncate text-xs text-slate-500">
                          {item.video_name || item.drive_file_id}
                        </p>
                        <p className="mt-1 text-[11px] text-slate-500">
                          v{item.copy_version ?? 1}
                          {item.layout_mode ? ` · ${item.layout_mode}` : ""}
                          {item.slide_count != null ? ` · ${item.slide_count} slides` : ""}
                          {item.created_at ? ` · ${formatWhen(item.created_at)}` : ""}
                        </p>
                        <div className="mt-2 flex flex-wrap gap-2">
                          <Link
                            href={studioHref}
                            className="rounded-md bg-slate-900 px-2.5 py-1.5 text-xs font-medium text-white hover:bg-slate-800"
                          >
                            Open / Edit
                          </Link>
                          <Link
                            href={testHref}
                            className="rounded-md border border-slate-200 px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
                          >
                            Test studio
                          </Link>
                        </div>
                      </div>
                    </li>
                  );
                })}
              </ul>
              {savesError ? (
                <p className="mt-2 text-xs text-red-700" role="alert">
                  {savesError}
                </p>
              ) : null}
              {savesHasMore ? (
                <div className="mt-4 flex justify-center">
                  <button
                    type="button"
                    disabled={savesLoadingMore}
                    onClick={() => void loadSaves({ append: true, offset: savesOffset })}
                    className="rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-60"
                  >
                    {savesLoadingMore ? (
                      <LoadingLabel>Loading more…</LoadingLabel>
                    ) : (
                      "Load more"
                    )}
                  </button>
                </div>
              ) : null}
            </>
          )}
        </section>

        {youtube.length > 0 && (
          <section className="mb-10">
            <h2 className="mb-3 text-sm font-semibold text-slate-800">YouTube downloads</h2>
            <ul className="divide-y divide-slate-200 overflow-hidden rounded-xl border border-slate-200 bg-white/90">
              {youtube.map((v) => (
                <li key={v.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                  <Film size={16} className="shrink-0 text-slate-400" />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-slate-900">{v.name}</p>
                    <p className="truncate text-xs text-slate-500">
                      {v.status}
                      {v.size != null ? ` · ${(v.size / (1024 * 1024)).toFixed(1)} MB` : ""}
                    </p>
                  </div>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      className="rounded-md border border-slate-200 px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
                      onClick={() => void downloadFromUrl(driveFileDownloadUrl(v.id), v.name)}
                    >
                      Download
                    </button>
                    <Link
                      href={`/carousel?video=${encodeURIComponent(v.id)}`}
                      className="rounded-md bg-slate-900 px-2.5 py-1.5 text-xs font-medium text-white hover:bg-slate-800"
                    >
                      Open in studio
                    </Link>
                  </div>
                </li>
              ))}
            </ul>
          </section>
        )}

        <section>
          <h2 className="mb-3 text-sm font-semibold text-slate-800">All videos</h2>
          {loading && videos.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-2 rounded-xl bg-slate-50 px-4 py-8 text-center">
              <p className="text-sm text-slate-500">
                <LoadingLabel>Loading library…</LoadingLabel>
              </p>
            </div>
          ) : videos.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-2 rounded-xl bg-slate-50 px-4 py-8 text-center">
              <p className="text-sm text-slate-500">No videos in the library yet.</p>
            </div>
          ) : (
            <ul className="divide-y divide-slate-200 overflow-hidden rounded-xl border border-slate-200 bg-white/90">
              {videos.map((v) => (
                <li key={v.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                  <Film size={16} className="shrink-0 text-slate-400" />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-slate-900">{v.name}</p>
                    <p className="truncate text-xs text-slate-500">
                      {v.has_captions !== false
                        ? `${v.cue_count ?? "…"} cues`
                        : "No captions"}
                      {v.status ? ` · ${v.status}` : ""}
                    </p>
                  </div>
                  <Link
                    href={`/carousel?video=${encodeURIComponent(v.id)}`}
                    className="rounded-md bg-slate-900 px-2.5 py-1.5 text-xs font-medium text-white hover:bg-slate-800"
                  >
                    Open in studio
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>
    </div>
  );
}
