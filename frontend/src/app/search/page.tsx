"use client";

import { useEffect, useState } from "react";
import {
  apiAssetUrl,
  apiClient,
  API_BASE,
  driveFilePreviewUrl,
  type FolderContext,
  type Person,
  type SearchMoment,
  type SearchResponse,
  type SearchResultFile,
} from "@/lib/api";
import { Button, Card, FilePreview, Input } from "@/components/ui";

function formatTimestamp(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export default function SearchPage() {
  const [q, setQ] = useState("");
  const [person, setPerson] = useState("");
  const [mime, setMime] = useState("all");
  const [folderPath, setFolderPath] = useState("");
  const [rerank, setRerank] = useState(true);
  const [persons, setPersons] = useState<Person[]>([]);
  const [folderContexts, setFolderContexts] = useState<FolderContext[]>([]);
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [previewFile, setPreviewFile] = useState<SearchResultFile | null>(null);

  useEffect(() => {
    apiClient.persons().then(setPersons).catch(() => setPersons([]));
    apiClient.folderContexts().then(setFolderContexts).catch(() => {});
  }, []);

  async function search() {
    if (!q.trim()) return;
    setLoading(true);
    setError(null);
    setPreviewFile(null);
    try {
      const params = new URLSearchParams({ q: q.trim() });
      if (person) params.set("person", person);
      if (mime !== "all") params.set("mime", mime);
      if (folderPath) params.set("folder_path", folderPath);
      if (!rerank) params.set("rerank", "false");
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

  const files = results?.files ?? [];
  const moments = results?.moments ?? [];

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold sm:text-2xl">Search</h2>
        <p className="text-sm text-zinc-400">
          Visual search for objects, actions, poses, expressions, and scenes. Videos use Gemini Embedding 2
          (frame-level, exact timestamps). Images use Gemini.
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
        <div className="flex flex-col gap-2 sm:flex-row">
          <Button className="w-full sm:w-auto" onClick={search} disabled={loading}>
            {loading ? "Searching…" : "Search"}
          </Button>
          <button
            type="button"
            onClick={() => setRerank((v) => !v)}
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

      {error && <Card className="border-destructive text-destructive">{error}</Card>}

      {results && moments.length > 0 && (
        <Card>
          <h3 className="mb-4 font-medium">Video moments ({moments.length})</h3>
          <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {moments.map((moment) => (
              <MomentCard key={`${moment.drive_file_id}-${moment.timestamp_sec}`} moment={moment} />
            ))}
          </ul>
        </Card>
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
                    <div className="px-3 py-3 text-sm">
                      <a
                        href={driveUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="font-medium text-primary hover:underline"
                      >
                        {file.name}
                      </a>
                      <p className="mt-1 truncate text-xs text-muted-foreground" title={file.path}>
                        {file.path}
                      </p>
                      {(file.person_names ?? []).length > 0 && (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {(file.person_names ?? []).map((name) => (
                            <span
                              key={name}
                              className="rounded-full bg-primary/15 px-2 py-0.5 text-xs text-primary"
                            >
                              {name}
                            </span>
                          ))}
                        </div>
                      )}
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
            className="relative max-h-[90vh] max-w-5xl overflow-hidden rounded-lg bg-card"
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
              src={driveFilePreviewUrl(previewFile.drive_file_id)}
              alt={previewFile.name}
              className="max-h-[85vh] max-w-full object-contain"
            />
            <p className="border-t border-border px-4 py-2 text-sm text-zinc-300">{previewFile.name}</p>
          </div>
        </div>
      )}
    </div>
  );
}

function matchBadgeStyle(matchType: string): string {
  if (matchType === "face_detected") return "bg-amber-950 text-amber-300";
  if (matchType === "gemini_visual") return "bg-violet-950 text-violet-300";
  if (matchType.startsWith("svs_visual")) return "bg-violet-950 text-violet-300";
  if (matchType.startsWith("svs_transcript")) return "bg-blue-950 text-blue-300";
  if (matchType === "transcript") return "bg-slate-800 text-slate-300";
  return "bg-zinc-800 text-zinc-300";
}

function matchLabel(matchType: string, score: number | null): string {
  const pct = score != null ? ` ${Math.round(score * 100)}%` : "";
  if (matchType === "face_detected") return `face${pct}`;
  if (matchType === "gemini_visual") return `visual${pct}`;
  if (matchType === "svs_visual") return `visual${pct}`;
  if (matchType === "svs_transcript") return `transcript${pct}`;
  return `${matchType}${pct}`;
}

function MomentCard({ moment }: { moment: SearchMoment }) {
  const seekUrl = moment.video_url ? apiAssetUrl(moment.video_url) : null;
  const isFace = moment.match_type === "face_detected";

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
          {formatTimestamp(moment.timestamp_sec)}
        </span>
        {isFace && (
          <span className="absolute right-2 top-2 rounded bg-amber-600/80 px-2 py-0.5 text-xs text-white">
            👤 face match
          </span>
        )}
      </div>
      <div className="px-3 py-3 text-sm">
        <p className="font-medium">{moment.name}</p>
        <p className="mt-1 truncate text-xs text-muted-foreground" title={moment.path}>
          {moment.path}
        </p>
        {moment.snippet && (
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
              Play at {formatTimestamp(moment.timestamp_sec)}
            </a>
          )}
        </div>
        {(moment.person_names ?? []).length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1">
            {(moment.person_names ?? []).map((name) => (
              <span key={name} className="rounded-full bg-primary/15 px-2 py-0.5 text-xs text-primary">
                {name}
              </span>
            ))}
          </div>
        )}
      </div>
    </li>
  );
}
