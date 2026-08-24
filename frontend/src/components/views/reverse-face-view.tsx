"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";
import {
  ArrowLeft,
  Check,
  ExternalLink,
  FolderOpen,
  Link2,
  ScanFace,
  Tag,
  Upload,
  Users,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  driveFilePreviewUrl,
  type FaceSearchAppearance,
  type FaceSearchMatch,
  type FaceSearchResponse,
  type LeadershipPerson,
} from "@/lib/api";
import { Button, Card, ConfirmDialog, FaceThumb, LoadingLabel } from "@/components/ui";
import { cn } from "@/lib/utils";
import {
  clearReverseFaceSearch,
  hydrateLeadershipRoster,
  patchReverseFaceSession,
  runReverseFaceCrawl,
  runReverseFaceNameTag,
  runReverseFaceSearch,
  selectReverseFaceLeader,
  setReverseFaceFile,
  totalMatchFileCount,
  useReverseFaceSession,
} from "@/lib/reverse-face-session";

function collectAppearances(matches: FaceSearchMatch[]): FaceSearchAppearance[] {
  const seen = new Set<string>();
  const out: FaceSearchAppearance[] = [];
  for (const m of matches) {
    for (const a of m.appears_in ?? []) {
      if (seen.has(a.drive_file_id)) continue;
      seen.add(a.drive_file_id);
      out.push(a);
    }
  }
  return out;
}

function collectClusters(matches: FaceSearchMatch[]) {
  const seen = new Set<number>();
  const out: {
    cluster_id: number;
    status: string | null;
    member_count: number | null;
    file_count: number | null;
    face_id: number;
    person_id: number | null;
    person_name: string;
    score: number;
  }[] = [];
  for (const m of matches) {
    if (m.cluster_id == null) continue;
    if (seen.has(m.cluster_id)) continue;
    seen.add(m.cluster_id);
    out.push({
      cluster_id: m.cluster_id,
      status: m.cluster_status ?? null,
      member_count: m.cluster_member_count ?? null,
      file_count: m.file_count ?? null,
      face_id: m.face_id,
      person_id: m.person_id,
      person_name: m.person_name,
      score: m.score,
    });
  }
  return out;
}

function MatchRow({ match }: { match: FaceSearchMatch }) {
  const pathname = usePathname();
  const personBase = pathname.startsWith("/test") ? "/test/people" : "/people";
  return (
    <div className="flex items-center gap-3 rounded-lg bg-muted/30 p-2.5">
      <FaceThumb faceId={match.face_id} className="h-12 w-12 shrink-0 rounded-md" />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium leading-snug text-foreground">{match.person_name}</p>
        <p className="text-[11px] text-muted-foreground">
          {Math.round(match.score * 100)}%
          {match.cluster_id != null ? ` · cluster #${match.cluster_id}` : ""}
          {match.person_id != null ? ` · person #${match.person_id}` : ""}
        </p>
        {match.linkedin_url && (
          <a
            href={match.linkedin_url}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-0.5 inline-flex items-center gap-1 text-[11px] text-sky-700 underline-offset-2 hover:underline dark:text-sky-300"
          >
            LinkedIn
            <ExternalLink size={10} aria-hidden />
          </a>
        )}
      </div>
      {match.person_id != null && (
        <Link
          href={`${personBase}/${match.person_id}`}
          className="shrink-0 text-[11px] font-medium text-muted-foreground hover:text-foreground"
        >
          Profile
        </Link>
      )}
    </div>
  );
}

function ResultsSidePanel({
  result,
  leader,
  tagging,
  tagMessage,
  onNameTag,
}: {
  result: FaceSearchResponse;
  leader: LeadershipPerson | null;
  tagging: boolean;
  tagMessage: string | null;
  onNameTag: () => void;
}) {
  const clusters = useMemo(() => collectClusters(result.matches), [result.matches]);
  const files = useMemo(() => collectAppearances(result.matches), [result.matches]);
  const totalFileCount = useMemo(() => totalMatchFileCount(result.matches), [result.matches]);
  const canTag =
    !!leader &&
    result.matches.length > 0 &&
    (clusters.some((c) => (c.status ?? "").toLowerCase() === "unknown" || c.person_id == null) ||
      result.matches.some((m) => m.person_id == null));

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
      <section className="min-w-0 space-y-3">
        <div className="flex items-start justify-between gap-2">
          <div>
            <h3 className="text-sm font-medium">Matches</h3>
            <p className="text-xs text-muted-foreground">
              {result.faces_detected} face{result.faces_detected === 1 ? "" : "s"} detected
              {result.query_confidence != null
                ? ` · confidence ${Math.round(result.query_confidence * 100)}%`
                : ""}
              {result.message ? ` · ${result.message}` : ""}
            </p>
          </div>
          {leader && (
            <div className="flex shrink-0 items-center gap-2 rounded-md bg-muted/40 px-2 py-1">
              {leader.image_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={leader.image_url}
                  alt=""
                  className="h-8 w-8 rounded object-cover"
                  referrerPolicy="no-referrer"
                />
              ) : null}
              <div className="min-w-0">
                <p className="max-w-[9rem] truncate text-[11px] font-medium text-foreground">
                  {leader.name}
                </p>
                <p className="max-w-[9rem] truncate text-[10px] text-muted-foreground">{leader.role}</p>
              </div>
            </div>
          )}
        </div>

        {leader && result.matches.length > 0 && (
          <div className="rounded-lg bg-muted/30 p-3">
            <p className="mb-2 text-xs text-muted-foreground">
              Auto name-tag matched clusters/faces as <span className="font-medium text-foreground">{leader.name}</span>{" "}
              (from mastersunion.org).
            </p>
            <Button
              onClick={onNameTag}
              disabled={!canTag || tagging}
              className="gap-1.5 px-2.5 py-1.5 text-xs"
            >
              {tagging ? (
                <LoadingLabel>Tagging…</LoadingLabel>
              ) : (
                <>
                  <Tag size={13} aria-hidden />
                  Name-tag as {leader.name}
                </>
              )}
            </Button>
            {tagMessage && (
              <p className="mt-2 flex items-start gap-1 text-xs text-emerald-700 dark:text-emerald-300">
                <Check size={12} className="mt-0.5 shrink-0" aria-hidden />
                {tagMessage}
              </p>
            )}
            {!canTag && !tagMessage && (
              <p className="mt-2 text-[11px] text-muted-foreground">
                All matches already have person links — nothing left to auto-tag.
              </p>
            )}
          </div>
        )}

        {result.matches.length === 0 ? (
          <p className="text-sm text-muted-foreground">No matches above the similarity threshold.</p>
        ) : (
          <div className="space-y-2">
            {result.matches.map((m) => (
              <MatchRow key={`${m.face_id}-${m.person_id ?? "x"}`} match={m} />
            ))}
          </div>
        )}
      </section>

      <div className="min-w-0 space-y-6">
        <section className="space-y-3">
          <h3 className="flex items-center gap-1.5 text-sm font-medium">
            <Users size={14} className="text-muted-foreground" aria-hidden />
            Clusters
          </h3>
          {clusters.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              No unknown/linked clusters on these matches (named faces may already be attached to a person).
            </p>
          ) : (
            <div className="space-y-2">
              {clusters.map((c) => (
                <div
                  key={c.cluster_id}
                  className="flex items-center gap-3 rounded-lg bg-muted/30 p-2.5"
                >
                  <FaceThumb faceId={c.face_id} className="h-11 w-11 shrink-0 rounded-md" />
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium">Cluster #{c.cluster_id}</p>
                    <p className="text-[11px] text-muted-foreground">
                      {c.status ?? "unknown"}
                      {c.member_count != null ? ` · ${c.member_count} faces` : ""}
                      {c.file_count != null && c.file_count > 0
                        ? ` · ${c.file_count} file${c.file_count === 1 ? "" : "s"}`
                        : ""}
                      {` · ${Math.round(c.score * 100)}%`}
                      {c.person_name !== "Unknown" ? ` · ${c.person_name}` : ""}
                    </p>
                  </div>
                  <Link
                    href="/review"
                    className="shrink-0 text-[11px] text-muted-foreground hover:text-foreground"
                  >
                    Review
                  </Link>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="space-y-3">
          <h3 className="flex items-center gap-1.5 text-sm font-medium">
            <FolderOpen size={14} className="text-muted-foreground" aria-hidden />
            Files where they appear
            {totalFileCount > 0 ? (
              <span className="font-normal text-muted-foreground">({totalFileCount})</span>
            ) : null}
          </h3>
          {files.length === 0 ? (
            <p className="text-xs text-muted-foreground">No Drive files linked to these matches yet.</p>
          ) : (
            <ul className="max-h-[28rem] space-y-0.5 overflow-y-auto pr-1">
              {files.map((f) => (
                <li key={f.drive_file_id}>
                  <a
                    href={driveFilePreviewUrl(f.drive_file_id)}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex items-start gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-muted/50"
                  >
                    <ExternalLink size={12} className="mt-0.5 shrink-0 text-muted-foreground" aria-hidden />
                    <span className="min-w-0">
                      <span className="block text-xs font-medium leading-snug text-foreground">{f.name}</span>
                      <span className="block break-all text-[10px] text-muted-foreground">
                        {f.path || f.media_type}
                        {f.frame_timestamp != null ? ` · ${f.frame_timestamp.toFixed(1)}s` : ""}
                      </span>
                    </span>
                  </a>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}

export function ReverseFaceLabPage({ embedded = false }: { embedded?: boolean } = {}) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const {
    dragOver,
    file,
    previewUrl,
    searching,
    result,
    crawlUrls,
    crawling,
    crawlResult,
    error,
    roster,
    rosterLoading,
    selectedLeader,
    tagging,
    tagMessage,
    confirmTagOpen,
  } = useReverseFaceSession();

  useEffect(() => {
    void hydrateLeadershipRoster();
  }, []);

  const setSelectedFile = useCallback((next: File | null) => {
    setReverseFaceFile(next);
  }, []);

  async function runSearch(upload?: File) {
    await runReverseFaceSearch(upload);
  }

  async function selectLeader(person: LeadershipPerson) {
    await selectReverseFaceLeader(person);
  }

  async function runNameTag() {
    if (!selectedLeader || !result?.matches.length) return;
    await runReverseFaceNameTag({
      name: selectedLeader.name,
      role: selectedLeader.role || null,
    });
  }

  async function runCrawl() {
    await runReverseFaceCrawl();
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      {!embedded && (
      <div className="flex items-start gap-3">
        <Link
          href="/labs/reid"
          className="mt-1 inline-flex h-8 w-8 items-center justify-center rounded-lg border border-border text-muted-foreground hover:bg-muted hover:text-foreground"
          aria-label="Back"
        >
          <ArrowLeft size={16} />
        </Link>
        <div>
          <h2 className="flex items-center gap-2 text-xl font-semibold sm:text-2xl">
            <ScanFace size={22} className="text-muted-foreground" aria-hidden />
            Reverse Face Search
          </h2>
          <p className="text-sm text-muted-foreground">
            Pick an Executive Leader mini card or upload a face photo. Matches show clusters and Drive files sideways.
          </p>
        </div>
      </div>
      )}

      <section className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h3 className="text-sm font-medium">Masters&apos; Union — Executive Leaders</h3>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Live roster from{" "}
              <a
                href="https://mastersunion.org/about-us"
                target="_blank"
                rel="noopener noreferrer"
                className="text-sky-700 underline-offset-2 hover:underline dark:text-sky-300"
              >
                mastersunion.org/about-us
              </a>
              {roster ? ` · ${roster.count} leaders` : ""}. Select a card to reverse-lookup.
            </p>
          </div>
          <Button variant="secondary" onClick={() => void hydrateLeadershipRoster(true)} disabled={rosterLoading} className="px-2.5 py-1.5 text-xs">
            {rosterLoading ? <LoadingLabel>Loading…</LoadingLabel> : "Refresh roster"}
          </Button>
        </div>

        {rosterLoading && !roster ? (
          <p className="py-8 text-center text-sm text-muted-foreground">
            <LoadingLabel>Fetching Executive Leaders…</LoadingLabel>
          </p>
        ) : roster && roster.people.length > 0 ? (
          <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-7">
            {roster.people.map((person) => {
              const selected =
                selectedLeader?.name === person.name && selectedLeader?.image_url === person.image_url;
              return (
                <button
                  key={`${person.name}-${person.image_url}`}
                  type="button"
                  disabled={searching || !person.image_url}
                  onClick={() => void selectLeader(person)}
                  className={cn(
                    "group flex flex-col items-center gap-1.5 rounded-xl p-2 text-center transition-colors",
                    selected
                      ? "bg-primary/10 ring-1 ring-primary/30"
                      : "hover:bg-muted/50",
                    (!person.image_url || searching) && "opacity-60"
                  )}
                >
                  <div className="relative h-16 w-16 overflow-hidden rounded-lg bg-muted sm:h-[4.5rem] sm:w-[4.5rem]">
                    {person.image_url ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={person.image_url}
                        alt={person.name}
                        className="h-full w-full object-cover"
                        referrerPolicy="no-referrer"
                      />
                    ) : (
                      <span className="flex h-full items-center justify-center text-[10px] text-muted-foreground">
                        No photo
                      </span>
                    )}
                    {selected && searching && (
                      <span className="absolute inset-0 flex items-center justify-center bg-background/50 text-[10px] font-medium">
                        …
                      </span>
                    )}
                  </div>
                  <span className="line-clamp-2 w-full text-[10px] font-medium leading-tight text-foreground sm:text-[11px]">
                    {person.name}
                  </span>
                  <span className="line-clamp-1 w-full text-[9px] text-muted-foreground">{person.role}</span>
                </button>
              );
            })}
          </div>
        ) : (
          <p className="py-6 text-center text-sm text-muted-foreground">No leaders returned from the scrape.</p>
        )}
      </section>

      <section className="space-y-3">
        <h3 className="text-sm font-medium">Upload face photo</h3>
        <p className="text-xs text-muted-foreground">
          Largest detected face is embedded with ArcFace and matched via pgvector.
        </p>
        <div
          className={cn(
            "flex flex-col items-center justify-center rounded-xl border-2 border-dashed px-4 py-10 text-center transition-colors",
            dragOver ? "border-primary/60 bg-primary/5" : "border-border/70 bg-muted/10"
          )}
          onDragOver={(e) => {
            e.preventDefault();
            patchReverseFaceSession({ dragOver: true });
          }}
          onDragLeave={() => patchReverseFaceSession({ dragOver: false })}
          onDrop={(e) => {
            e.preventDefault();
            patchReverseFaceSession({ dragOver: false });
            const dropped = e.dataTransfer.files?.[0];
            if (dropped) setSelectedFile(dropped);
          }}
        >
          {previewUrl ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={previewUrl}
              alt="Upload preview"
              className="mb-3 max-h-40 rounded-lg object-contain"
            />
          ) : (
            <Upload size={28} className="mb-3 text-muted-foreground" aria-hidden />
          )}
          <p className="text-sm text-muted-foreground">
            {file ? file.name : "Drop an image here, or choose a file"}
          </p>
          <div className="mt-3 flex flex-wrap justify-center gap-2">
            <Button variant="secondary" onClick={() => fileInputRef.current?.click()}>
              Choose file
            </Button>
            <Button onClick={() => runSearch()} disabled={!file || searching}>
              {searching && !selectedLeader ? <LoadingLabel>Searching…</LoadingLabel> : "Search faces"}
            </Button>
          </div>
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => {
              const next = e.target.files?.[0] ?? null;
              setSelectedFile(next);
              if (next) void runSearch(next);
            }}
          />
        </div>
      </section>

      {searching && !result && (
        <p className="py-6 text-center text-sm text-muted-foreground">
          <LoadingLabel>
            {selectedLeader ? `Searching for ${selectedLeader.name}…` : "Searching faces…"}
          </LoadingLabel>
        </p>
      )}

      {result && (
        <div className="relative">
          <div className="mb-3 flex justify-end">
            <button
              type="button"
              onClick={clearReverseFaceSearch}
              title="Clear search"
              aria-label="Clear search"
              className="inline-flex h-8 w-8 items-center justify-center rounded-full border border-border/70 bg-card text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              <X size={16} aria-hidden />
            </button>
          </div>
          <ResultsSidePanel
            result={result}
            leader={selectedLeader}
            tagging={tagging}
            tagMessage={tagMessage}
            onNameTag={() => patchReverseFaceSession({ confirmTagOpen: true })}
          />
        </div>
      )}

      <ConfirmDialog
        open={confirmTagOpen && !!selectedLeader}
        title="Auto name-tag"
        message={
          selectedLeader
            ? `Create or link Person “${selectedLeader.name}” using the website name, and name-tag matched unknown clusters/faces?`
            : ""
        }
        confirmLabel="Name-tag"
        variant="primary"
        onConfirm={() => void runNameTag()}
        onCancel={() => patchReverseFaceSession({ confirmTagOpen: false })}
      />

      <section className="space-y-3">
        <h3 className="flex items-center gap-1.5 text-sm font-medium">
          <Link2 size={14} aria-hidden />
          Crawl public image URLs
        </h3>
        <p className="text-xs text-muted-foreground">
          Optional MVP: fetch public images and match faces against the index (no private LinkedIn scraping).
        </p>
        <textarea
          className="min-h-[88px] w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          placeholder={"https://example.com/photo.jpg\nhttps://cdn.example.com/headshot.png"}
          value={crawlUrls}
          onChange={(e) => patchReverseFaceSession({ crawlUrls: e.target.value })}
        />
        <div>
          <Button onClick={runCrawl} disabled={crawling || !crawlUrls.trim()}>
            {crawling ? <LoadingLabel>Crawling…</LoadingLabel> : "Crawl & match"}
          </Button>
        </div>
        {crawlResult && (
          <div className="space-y-3 border-t border-border pt-4">
            <p className="text-xs text-muted-foreground">
              Crawled {crawlResult.crawled} URL{crawlResult.crawled === 1 ? "" : "s"}
            </p>
            {crawlResult.results.map((item) => (
              <div key={item.url} className="rounded-md bg-muted/30 p-3">
                <p className="break-all text-xs font-medium" title={item.url}>
                  {item.url}
                </p>
                {!item.ok ? (
                  <p className="mt-1 text-xs text-destructive">{item.error || "Failed"}</p>
                ) : (
                  <div className="mt-2 space-y-2">
                    <p className="text-xs text-muted-foreground">
                      {item.search?.faces_detected ?? 0} face(s) ·{" "}
                      {totalMatchFileCount(item.search?.matches ?? [])} match(es)
                    </p>
                    {(item.search?.matches ?? []).slice(0, 5).map((m) => (
                      <MatchRow key={`${item.url}-${m.face_id}`} match={m} />
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
