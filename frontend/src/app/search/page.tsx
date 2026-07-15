"use client";

import { useEffect, useState } from "react";
import {
  apiAssetUrl,
  apiClient,
  API_BASE,
  driveFileDownloadUrl,
  driveFilePreviewUrl,
  type FolderContext,
  type Person,
  type SearchMoment,
  type SearchResponse,
  type SearchResultFile,
} from "@/lib/api";
import { Button, Card, FilePreview, Input, ServiceErrorCard } from "@/components/ui";

function formatTimestamp(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function isTranscriptMatch(matchType: string): boolean {
  return (
    matchType === "transcript" ||
    matchType === "transcript_regex" ||
    matchType.startsWith("svs_transcript")
  );
}

function formatTimestampRange(start: number, end?: number | null): string {
  const startLabel = formatTimestamp(start);
  if (end != null && end > start + 0.5) {
    return `${startLabel}–${formatTimestamp(end)}`;
  }
  return startLabel;
}

export default function SearchPage() {
  const [q, setQ] = useState("");
  const [person, setPerson] = useState("");
  const [mime, setMime] = useState("all");
  const [folderPath, setFolderPath] = useState("");
  const [rerank, setRerank] = useState(true);
  const [useCaptions, setUseCaptions] = useState(false);
  const [persons, setPersons] = useState<Person[]>([]);
  const [folderContexts, setFolderContexts] = useState<FolderContext[]>([]);
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [lastSearchMode, setLastSearchMode] = useState<{ captions: boolean; rerank: boolean } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [previewFile, setPreviewFile] = useState<SearchResultFile | null>(null);

  useEffect(() => {
    apiClient.persons().then(setPersons).catch(() => setPersons([]));
    apiClient.folderContexts().then(setFolderContexts).catch(() => {});
    apiClient
      .settings()
      .then((s) => {
        setRerank(s.search_rerank_enabled);
        setUseCaptions(s.search_use_captions);
      })
      .catch(() => {});
  }, []);

  async function setRerankPersist(value: boolean) {
    setRerank(value);
    try {
      await apiClient.updateSettings({ search_rerank_enabled: value });
    } catch {
      /* keep local toggle; settings page can fix */
    }
  }

  async function setCaptionsPersist(value: boolean) {
    setUseCaptions(value);
    try {
      await apiClient.updateSettings({ search_use_captions: value });
    } catch {
      /* keep local toggle */
    }
  }

  async function search() {
    if (!q.trim()) return;
    setLoading(true);
    setError(null);
    setPreviewFile(null);
    setLastSearchMode({ captions: useCaptions, rerank });
    try {
      const params = new URLSearchParams({ q: q.trim() });
      if (person) params.set("person", person);
      if (mime !== "all") params.set("mime", mime);
      if (folderPath) params.set("folder_path", folderPath);
      if (!rerank) params.set("rerank", "false");
      if (useCaptions) params.set("captions", "true");
      const res = await fetch(`${API_BASE}/search?${params}`, { cache: "no-store" });
      if (!res.ok) throw new Error(await res.text());
      setResults(await res.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Search failed");
      setResults(null);
    } finally {
      setLoading(false);
    }
  }

  const files = (results?.files ?? []).filter(
    (f) => f.score != null || !f.mime_type.startsWith("image/")
  );
  const moments = results?.moments ?? [];

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold sm:text-2xl">Search</h2>
        <p className="text-sm text-zinc-400">
          Visual search via Gemini embeddings. Toggle captions for text-description matching (slower, stricter).
          Videos use frame search + optional re-ranking.
        </p>
      </div>

      <div className="max-w-3xl space-y-2">
        <Input
          className="w-full"
          placeholder="Search (e.g. wine glass, smiling, party, people)..."
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && search()}
        />
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          <select
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-ring"
            value={mime}
            onChange={(e) => setMime(e.target.value)}
          >
            <option value="all">All files</option>
            <option value="image">Images only</option>
            <option value="pdf">PDFs only</option>
            <option value="video">Videos only</option>
          </select>
          <select
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-ring"
            value={person}
            onChange={(e) => setPerson(e.target.value)}
            disabled={persons.length === 0}
          >
            <option value="">All people</option>
            {persons.map((p) => (
              <option key={p.id} value={p.name}>
                {p.name}
              </option>
            ))}
          </select>
          <select
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-ring sm:col-span-2"
            value={folderPath}
            onChange={(e) => setFolderPath(e.target.value)}
            title={folderPath && folderContexts.find(f => f.folder_path === folderPath)?.description}
          >
            <option value="">All folders</option>
            {folderContexts.map((f) => (
              <option key={f.folder_path} value={f.folder_path} title={f.description}>
                📁 {f.folder_path.split("/").filter(Boolean).pop() ?? f.folder_path}
                {f.description ? " ✦" : ""}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
          <Button className="w-full sm:w-auto" onClick={search} disabled={loading}>
            {loading ? "Searching…" : "Search"}
          </Button>
          <button
            type="button"
            onClick={() => setCaptionsPersist(!useCaptions)}
            title={useCaptions ? "Caption search ON — fuses indexed image descriptions" : "Caption search OFF — visual embeddings only"}
            className={`w-full rounded-md border px-3 py-2 text-xs font-medium transition-colors sm:w-auto ${
              useCaptions
                ? "border-primary/50 bg-primary/10 text-primary"
                : "border-border bg-muted text-muted-foreground"
            }`}
          >
            {useCaptions ? "Captions ON" : "Captions OFF"}
          </button>
          <button
            type="button"
            onClick={() => setRerankPersist(!rerank)}
            title={rerank ? "AI re-ranking ON — click to disable" : "AI re-ranking OFF — click to enable"}
            className={`w-full rounded-md border px-3 py-2 text-xs font-medium transition-colors sm:w-auto ${
              rerank
                ? "border-primary/50 bg-primary/10 text-primary"
                : "border-border bg-muted text-muted-foreground"
            }`}
          >
            {rerank ? "✦ Re-rank ON" : "Re-rank OFF"}
          </button>
        </div>
      </div>

      {folderPath && folderContexts.find(f => f.folder_path === folderPath)?.description && (
        <p className="text-xs text-muted-foreground">
          📁 Folder context: <span className="italic">{folderContexts.find(f => f.folder_path === folderPath)?.description}</span>
        </p>
      )}

      {loading && (
        <p className="text-sm text-muted-foreground">Searching indexed media… visual queries can take up to a minute.</p>
      )}

      {error && (
        <ServiceErrorCard message={error} onRetry={search} onDismiss={() => setError(null)} />
      )}

      {results && lastSearchMode && (
        <Card className="border-border/80 bg-muted/30 px-4 py-3 text-sm">
          <p className="font-medium text-foreground">Search mode for this query</p>
          <ul className="mt-1.5 space-y-1 text-xs text-muted-foreground">
            <li>
              {lastSearchMode.captions
                ? "Captions ON — results can match indexed image descriptions as well as visual embeddings."
                : "Captions OFF — visual embedding match only (faster; ignores caption text)."}
            </li>
            <li>
              {lastSearchMode.rerank
                ? "Re-rank ON — results are re-ordered by AI relevance (images and videos)."
                : "Re-rank OFF — raw vector similarity order (no AI re-ordering)."}
            </li>
          </ul>
        </Card>
      )}

      {results && moments.length > 0 && (
        <>
          {(() => {
            const transcriptMoments = moments.filter((m) => isTranscriptMatch(m.match_type));
            const otherMoments = moments.filter((m) => !isTranscriptMatch(m.match_type));
            return (
              <>
                {transcriptMoments.length > 0 && (
                  <Card>
                    <h3 className="mb-1 font-medium">Transcript matches ({transcriptMoments.length})</h3>
                    <p className="mb-4 text-xs text-muted-foreground">
                      Exact phrase/word match in video captions — frame shown at spoken timestamp
                    </p>
                    <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                      {transcriptMoments.map((moment) => (
                        <MomentCard
                          key={`t-${moment.drive_file_id}-${moment.timestamp_sec}`}
                          moment={moment}
                        />
                      ))}
                    </ul>
                  </Card>
                )}
                {otherMoments.length > 0 && (
                  <Card>
                    <h3 className="mb-4 font-medium">
                      {transcriptMoments.length > 0 ? "Visual moments" : "Video moments"} ({otherMoments.length})
                    </h3>
                    <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                      {otherMoments.map((moment) => (
                        <MomentCard
                          key={`v-${moment.drive_file_id}-${moment.timestamp_sec}`}
                          moment={moment}
                        />
                      ))}
                    </ul>
                  </Card>
                )}
              </>
            );
          })()}
        </>
      )}

      {results && mime !== "video" && (
        <Card>
          <h3 className="mb-4 font-medium">Matching files ({files.length})</h3>
          {files.length === 0 ? (
            <p className="text-sm text-muted-foreground">No matching files in your Drive index.</p>
          ) : (
            <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {files.map((file) => {
                const driveUrl = `https://drive.google.com/file/d/${file.drive_file_id}/view`;
                const downloadUrl = driveFileDownloadUrl(file.drive_file_id);
                const isImage = file.mime_type.startsWith("image/");
                return (
                  <li
                    key={file.drive_file_id}
                    className="overflow-hidden rounded-md border border-border bg-muted/30"
                  >
                    <div className="aspect-[4/3] w-full overflow-hidden bg-black/20">
                      <FilePreview
                        driveFileId={file.drive_file_id}
                        name={file.name}
                        mimeType={file.mime_type}
                        onClick={isImage ? () => setPreviewFile(file) : undefined}
                      />
                    </div>
                    <div className="flex flex-col gap-2 px-3 py-3 text-sm">
                      <div className="flex items-start justify-between gap-2">
                        <a
                          href={driveUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="min-w-0 flex-1 font-medium leading-snug text-primary hover:underline"
                        >
                          {file.name}
                        </a>
                        {file.score != null && (
                          <span className="shrink-0 rounded bg-violet-950/60 px-1.5 py-0.5 text-[10px] text-violet-300">
                            {Math.round(file.score * 100)}%
                          </span>
                        )}
                      </div>
                      <p className="truncate text-xs text-muted-foreground" title={file.path}>
                        {file.path}
                      </p>
                      {file.caption && (
                        <p className="line-clamp-2 text-xs text-muted-foreground/90" title={file.caption}>
                          {file.caption}
                        </p>
                      )}
                      {(file.person_names ?? []).length > 0 && (
                        <div className="flex min-h-6 flex-wrap items-center gap-1.5">
                          <span className="shrink-0 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                            People
                          </span>
                          {(file.person_names ?? []).map((name) => (
                            <span
                              key={name}
                              className="inline-flex items-center rounded-full bg-primary/15 px-2 py-0.5 text-xs leading-none text-primary"
                            >
                              {name}
                            </span>
                          ))}
                        </div>
                      )}
                      <div className="flex flex-wrap items-center gap-2 pt-0.5">
                        <a
                          href={downloadUrl}
                          download={file.name}
                          className="inline-flex items-center rounded-md bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground hover:brightness-110"
                        >
                          Download
                        </a>
                        <a
                          href={driveUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-xs text-muted-foreground hover:text-foreground hover:underline"
                        >
                          Open in Drive
                        </a>
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
      )}

      {results && mime === "video" && moments.length === 0 && (
        <Card>
          <p className="text-sm text-zinc-500">
            No matching video moments. Make sure the video is indexed
            (check the Folders page). Once indexed, frames are embedded with Gemini Embedding 2 automatically.
          </p>
        </Card>
      )}

      {previewFile && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
          onClick={() => setPreviewFile(null)}
        >
          <div
            className="relative inline-flex max-h-[90vh] max-w-[min(92vw,56rem)] flex-col overflow-hidden rounded-lg bg-card shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              type="button"
              onClick={() => setPreviewFile(null)}
              className="absolute right-3 top-3 z-10 rounded-md bg-black/60 px-2 py-1 text-sm text-white hover:bg-black/80"
            >
              Close
            </button>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={driveFilePreviewUrl(previewFile.drive_file_id, previewFile.mime_type)}
              alt={previewFile.name}
              className="block max-h-[78vh] w-auto max-w-full object-contain"
            />
            <div className="border-t border-border px-4 py-3">
              <p className="break-all text-sm font-medium text-foreground">{previewFile.name}</p>
              {previewFile.caption && (
                <p className="mt-1 text-xs text-muted-foreground">{previewFile.caption}</p>
              )}
              <div className="mt-3">
                <a
                  href={driveFileDownloadUrl(previewFile.drive_file_id)}
                  download={previewFile.name}
                  className="inline-flex items-center rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:brightness-110"
                >
                  Download
                </a>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function matchBadgeStyle(matchType: string): string {
  if (matchType === "face_detected") return "bg-amber-950 text-amber-300";
  if (isTranscriptMatch(matchType)) return "bg-blue-950 text-blue-300";
  if (matchType === "gemini_visual") return "bg-violet-950 text-violet-300";
  if (matchType.startsWith("svs_visual")) return "bg-violet-950 text-violet-300";
  return "bg-zinc-800 text-zinc-300";
}

function matchLabel(matchType: string, score: number | null): string {
  const pct = score != null ? ` ${Math.round(score * 100)}%` : "";
  if (matchType === "face_detected") return `face${pct}`;
  if (isTranscriptMatch(matchType)) return `transcript${pct}`;
  if (matchType === "gemini_visual") return `visual${pct}`;
  if (matchType === "svs_visual") return `visual${pct}`;
  return `${matchType}${pct}`;
}

function MomentCard({ moment }: { moment: SearchMoment }) {
  const seekUrl = moment.video_url
    ? moment.video_url.startsWith("http")
      ? moment.video_url
      : apiAssetUrl(moment.video_url)
    : null;
  const isFace = moment.match_type === "face_detected";
  const isTranscript = isTranscriptMatch(moment.match_type);
  const timeLabel = formatTimestampRange(moment.timestamp_sec, moment.end_timestamp_sec);

  return (
    <li className="overflow-hidden rounded-md border border-border bg-muted/30">
      <div className="relative aspect-video w-full overflow-hidden bg-black/40">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={apiAssetUrl(moment.preview_url)}
          alt={moment.name}
          className="h-full w-full object-cover"
        />
        <span className="absolute bottom-2 left-2 rounded bg-black/70 px-2 py-0.5 text-xs text-white">
          {timeLabel}
        </span>
        {isTranscript && (
          <span className="absolute right-2 top-2 rounded bg-blue-600/90 px-2 py-0.5 text-xs text-white">
            transcript
          </span>
        )}
        {isFace && (
          <span className="absolute right-2 top-2 rounded bg-amber-600/80 px-2 py-0.5 text-xs text-white">
            face match
          </span>
        )}
      </div>
      <div className="px-3 py-3 text-sm">
        <p className="font-medium">{moment.name}</p>
        <p className="mt-1 truncate text-xs text-muted-foreground" title={moment.path}>
          {moment.path}
        </p>
        {isTranscript && moment.snippet && (
          <p
            className="mt-2 line-clamp-3 rounded bg-blue-950/30 px-2 py-1.5 text-xs text-blue-100"
            title={moment.snippet}
          >
            &ldquo;{moment.snippet}&rdquo;
          </p>
        )}
        {!isTranscript && moment.snippet && (
          <p className="mt-2 line-clamp-2 text-xs text-muted-foreground/80" title={moment.snippet}>
            {moment.snippet}
          </p>
        )}
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <span className={`rounded-full px-2 py-0.5 text-xs ${matchBadgeStyle(moment.match_type)}`}>
            {matchLabel(moment.match_type, moment.score ?? null)}
          </span>
          {seekUrl && (
            <a
              href={seekUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-primary hover:underline"
            >
              {isTranscript ? `Play transcript at ${timeLabel}` : `Play at ${timeLabel}`}
            </a>
          )}
        </div>
        {(moment.person_names ?? []).length > 0 && (
          <div className="mt-2 flex min-h-6 flex-wrap items-center gap-1.5">
            <span className="shrink-0 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              People
            </span>
            {(moment.person_names ?? []).map((name) => (
              <span
                key={name}
                className="inline-flex items-center rounded-full bg-primary/15 px-2 py-0.5 text-xs leading-none text-primary"
              >
                {name}
              </span>
            ))}
          </div>
        )}
      </div>
    </li>
  );
}
