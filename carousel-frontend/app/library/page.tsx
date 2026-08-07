"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Film, RefreshCw } from "lucide-react";
import {
  apiClient,
  driveFileDownloadUrl,
  formatApiError,
  type CarouselRecentVideo,
  type YoutubeDriveFile,
} from "@/lib/api";
import { downloadFromUrl } from "@/lib/download";
import StudioLogo from "@/components/StudioLogo";
import { LoadingLabel, ServiceErrorCard } from "@/components/ui";

export default function LibraryPage() {
  const [videos, setVideos] = useState<CarouselRecentVideo[]>([]);
  const [youtube, setYoutube] = useState<YoutubeDriveFile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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

  useEffect(() => {
    void load();
  }, [load]);

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
              className="hidden text-sm font-medium text-slate-900 sm:inline"
              aria-current="page"
            >
              Library
            </span>
            <Link
              href="/carousel"
              className="inline-flex h-9 items-center rounded-lg border border-slate-900 bg-slate-900 px-4 text-sm font-medium text-white shadow-sm"
            >
              Studio
            </Link>
          </div>
        </div>
      </nav>

      <main className="relative z-10 mx-auto max-w-5xl px-4 pb-24 pt-8 sm:px-6 sm:pt-10">
        <header className="mb-8">
          <p className="text-[11px] font-medium uppercase tracking-[0.2em] text-slate-500">
            Library
          </p>
          <h1 className="mt-2 font-serif text-3xl tracking-tight text-slate-900 sm:text-4xl">
            Captioned &amp; YouTube videos
          </h1>
          <p className="mt-2 max-w-xl text-sm text-slate-500">
            Browse indexed videos for carousel studio, or open a YouTube download from the shared
            library.
          </p>
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            className="mt-4 inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-60"
          >
            <RefreshCw size={14} className={loading ? "animate-spin" : undefined} />
            {loading ? <LoadingLabel>Refreshing…</LoadingLabel> : "Refresh"}
          </button>
        </header>

        {error && (
          <ServiceErrorCard
            message={error}
            onDismiss={() => setError(null)}
            onRetry={() => void load()}
            retrying={loading}
            retryLabel="Reload library"
          />
        )}

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
            <p className="text-sm text-slate-500">
              <LoadingLabel>Loading library…</LoadingLabel>
            </p>
          ) : videos.length === 0 ? (
            <p className="text-sm text-slate-500">No videos in the library yet.</p>
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
