"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Film, RefreshCw } from "lucide-react";
import { formatApiError } from "@/lib/api";
import { testApi, type TestVideo } from "@/lib/test-api";

export default function TestLibraryPage() {
  const [videos, setVideos] = useState<TestVideo[]>([]);
  const [youtube, setYoutube] = useState<TestVideo[]>([]);
  const [loading, setLoading] = useState(true);
  const [, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [lib, yt] = await Promise.all([
        testApi.allVideos(),
        testApi.youtubeVideos().catch(() => [] as TestVideo[]),
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
    <div className="mx-auto max-w-5xl px-4 pb-24 pt-8 sm:px-6 sm:pt-10">
      <header className="mb-8">
        <p className="text-[11px] font-medium uppercase tracking-[0.2em] text-slate-500">
          Test · Library
        </p>
        <h1 className="mt-2 font-serif text-3xl tracking-tight text-slate-900 sm:text-4xl">
          Add existing project
        </h1>
        <p className="mt-2 max-w-xl text-sm text-slate-500">
          Pick an indexed video to open in the redesigned studio flow.
        </p>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          className="mt-4 inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-60"
        >
          <RefreshCw size={14} className={loading ? "animate-spin" : undefined} />
          {loading ? "Refreshing…" : "Refresh"}
          </button>
      </header>

      {youtube.length > 0 && (
        <section className="mb-10">
          <h2 className="mb-3 text-sm font-semibold text-slate-800">YouTube</h2>
          <ul className="divide-y divide-slate-200 overflow-hidden rounded-xl border border-slate-200 bg-white/90">
            {youtube.map((v) => (
              <li key={v.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                <Film size={16} className="shrink-0 text-slate-400" />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-slate-900">{v.name}</p>
                  <p className="truncate text-xs text-slate-500">{v.status}</p>
                </div>
                <Link
                  href={`/test/studio?video=${encodeURIComponent(v.id)}`}
                  className="rounded-md bg-slate-900 px-2.5 py-1.5 text-xs font-medium text-white hover:bg-slate-800"
                >
                  Open in studio
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section>
        <h2 className="mb-3 text-sm font-semibold text-slate-800">All videos</h2>
        {loading && videos.length === 0 ? (
          <p className="text-sm text-slate-500">Loading library…</p>
        ) : videos.length === 0 ? (
          <p className="text-sm text-slate-500">No videos yet.</p>
        ) : (
          <ul className="divide-y divide-slate-200 overflow-hidden rounded-xl border border-slate-200 bg-white/90">
            {videos.map((v) => (
              <li key={v.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                <Film size={16} className="shrink-0 text-slate-400" />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-slate-900">{v.name}</p>
                  <p className="truncate text-xs text-slate-500">
                    {v.has_captions !== false ? `${v.cue_count ?? "…"} cues` : "No captions"}
                  </p>
                </div>
                <Link
                  href={`/test/studio?video=${encodeURIComponent(v.id)}`}
                  className="rounded-md bg-slate-900 px-2.5 py-1.5 text-xs font-medium text-white hover:bg-slate-800"
                  data-testid="test-open-video"
                >
                  Open in studio
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
