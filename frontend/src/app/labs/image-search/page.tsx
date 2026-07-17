"use client";

import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, ExternalLink, Image as ImageIcon, KeyRound, RefreshCw, Search } from "lucide-react";
import Link from "next/link";
import {
  apiClient,
  faceThumbnailUrl,
  type OfficialImageSearchResult,
  type OfficialImageSearchStatus,
} from "@/lib/api";
import { Button, Card, Input, LoadingLabel, ServiceErrorCard } from "@/components/ui";

function ResultLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex min-w-0 items-center gap-1 text-sky-700 underline-offset-2 hover:underline dark:text-sky-300"
    >
      <span className="truncate">{children}</span>
      <ExternalLink size={12} className="shrink-0" aria-hidden />
    </a>
  );
}

function Score({ value }: { value?: number | null }) {
  if (typeof value !== "number") return null;
  return <span className="text-[10px] text-muted-foreground">{Math.round(value * 100)}%</span>;
}

function ImageStrip({ title, items }: { title: string; items: { url: string; score?: number | null }[] }) {
  if (!items.length) return null;
  return (
    <Card className="space-y-3">
      <h3 className="text-sm font-semibold">{title}</h3>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {items.slice(0, 10).map((item) => (
          <a key={item.url} href={item.url} target="_blank" rel="noopener noreferrer" className="group block space-y-1">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={item.url}
              alt={title}
              className="aspect-square w-full rounded-lg border border-border bg-muted object-cover transition group-hover:opacity-80"
            />
            <p className="truncate text-[10px] text-muted-foreground">{item.url}</p>
          </a>
        ))}
      </div>
    </Card>
  );
}

export default function OfficialImageSearchLabPage() {
  const [status, setStatus] = useState<OfficialImageSearchStatus | null>(null);
  const [faceId, setFaceId] = useState("23032");
  const [imageUrl, setImageUrl] = useState("");
  const [maxResults, setMaxResults] = useState("10");
  const [result, setResult] = useState<OfficialImageSearchResult | null>(null);
  const [loadingStatus, setLoadingStatus] = useState(true);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    setLoadingStatus(true);
    try {
      setStatus(await apiClient.officialImageSearchStatus());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load image-search status");
    } finally {
      setLoadingStatus(false);
    }
  }, []);

  useEffect(() => {
    loadStatus();
  }, [loadStatus]);

  async function runSearch() {
    const trimmedUrl = imageUrl.trim();
    const id = Number(faceId);
    const limit = Number(maxResults);
    if (!trimmedUrl && (!Number.isFinite(id) || id <= 0)) {
      setError("Enter a face id or a public image URL");
      return;
    }

    setSearching(true);
    setError(null);
    try {
      const payload = trimmedUrl
        ? { image_url: trimmedUrl, max_results: Number.isFinite(limit) ? limit : 10 }
        : { face_id: id, max_results: Number.isFinite(limit) ? limit : 10 };
      setResult(await apiClient.officialImageSearch(payload));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Official image search failed");
    } finally {
      setSearching(false);
    }
  }

  const preview = imageUrl.trim() || faceThumbnailUrl(Number(faceId));

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <Link href="/" className="mb-2 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
            <ArrowLeft size={12} aria-hidden />
            Back
          </Link>
          <h2 className="flex items-center gap-2 text-xl font-semibold sm:text-2xl">
            <Search size={20} className="text-sky-500" aria-hidden />
            Official Image Search Lab
          </h2>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            Experimental Google Cloud Vision <strong>WEB_DETECTION</strong> reverse-image search. It uses
            an indexed face thumbnail or a public image URL and shows Google&apos;s best guesses, web entities,
            matching pages, and similar images.
          </p>
        </div>
        <Button variant="secondary" disabled={loadingStatus || searching} onClick={loadStatus}>
          <span className="inline-flex items-center gap-1.5">
            <RefreshCw size={14} aria-hidden />
            Refresh status
          </span>
        </Button>
      </div>

      <Card className="space-y-3">
        <p className="flex items-center gap-2 text-sm font-medium">
          <KeyRound size={16} className="text-amber-600" aria-hidden />
          Official Google API configuration
        </p>
        {loadingStatus ? (
          <p className="text-sm text-muted-foreground">
            <LoadingLabel size={16}>Checking key…</LoadingLabel>
          </p>
        ) : status ? (
          <div className="space-y-2 text-sm text-muted-foreground">
            <p>
              Configured: <strong className={status.configured ? "text-emerald-600" : "text-amber-600"}>
                {status.configured ? "yes" : "no"}
              </strong>
              {status.key_source && <> via <strong>{status.key_source}</strong></>}
            </p>
            <p>
              API keys have no OAuth scopes. If you use OAuth/service account credentials instead, use{" "}
              <code className="rounded bg-muted px-1 py-0.5">{status.scope_required_if_using_oauth}</code>.
            </p>
            <p>
              If it fails with 403/API not enabled, turn on{" "}
              <ResultLink href={status.enable_url}>Cloud Vision API</ResultLink> and billing in the key&apos;s
              Google Cloud project.
            </p>
          </div>
        ) : null}
      </Card>

      <Card className="space-y-4">
        <div className="grid gap-3 lg:grid-cols-[1fr_1fr_auto]">
          <label className="space-y-1">
            <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Face id</span>
            <Input value={faceId} onChange={(e) => setFaceId(e.target.value)} placeholder="23032" />
          </label>
          <label className="space-y-1">
            <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Public image URL</span>
            <Input value={imageUrl} onChange={(e) => setImageUrl(e.target.value)} placeholder="https://..." />
          </label>
          <label className="space-y-1">
            <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Max</span>
            <Input value={maxResults} onChange={(e) => setMaxResults(e.target.value)} className="lg:w-20" />
          </label>
        </div>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <Button disabled={searching} onClick={runSearch}>
            {searching ? <LoadingLabel>Searching…</LoadingLabel> : "Run official Google search"}
          </Button>
          <p className="text-xs text-muted-foreground">
            URL wins over face id. Face id sends the local thumbnail bytes directly to Vision.
          </p>
        </div>
        {preview && (
          <div className="flex items-center gap-3 rounded-lg border border-border bg-muted/30 p-3">
            <ImageIcon size={16} className="shrink-0 text-muted-foreground" aria-hidden />
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={preview} alt="search input preview" className="h-20 w-20 rounded-md object-cover" />
            <p className="min-w-0 truncate text-xs text-muted-foreground">{preview}</p>
          </div>
        )}
      </Card>

      {error && <ServiceErrorCard message={error} onRetry={runSearch} onDismiss={() => setError(null)} />}

      {result && (
        <div className="space-y-4">
          <Card className="space-y-3">
            <p className="text-xs text-muted-foreground">
              Provider <strong>{result.provider}</strong> · key <strong>{result.key_source}</strong>
            </p>
            {result.best_guess_labels.length > 0 && (
              <div>
                <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Best guess</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {result.best_guess_labels.map((label) => (
                    <span key={label} className="rounded-full bg-sky-500/15 px-2.5 py-1 text-xs font-semibold text-sky-700 dark:text-sky-300">
                      {label}
                    </span>
                  ))}
                </div>
              </div>
            )}
            {result.web_entities.length > 0 && (
              <div>
                <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Web entities</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {result.web_entities.slice(0, 16).map((entity, index) => (
                    <span key={`${entity.entity_id ?? entity.description ?? index}`} className="rounded-full border border-border bg-card px-2.5 py-1 text-xs">
                      {entity.description ?? entity.entity_id} <Score value={entity.score} />
                    </span>
                  ))}
                </div>
              </div>
            )}
          </Card>

          {result.pages_with_matching_images.length > 0 && (
            <Card className="space-y-3">
              <h3 className="text-sm font-semibold">Pages with matching images</h3>
              <div className="space-y-2">
                {result.pages_with_matching_images.slice(0, 10).map((page) => (
                  <div key={page.url} className="rounded-lg border border-border bg-muted/20 p-3 text-sm">
                    <ResultLink href={page.url}>{page.page_title || page.url}</ResultLink>
                    <div className="mt-1">
                      <Score value={page.score} />
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          )}

          <ImageStrip title="Full matching images" items={result.full_matching_images} />
          <ImageStrip title="Partial matching images" items={result.partial_matching_images} />
          <ImageStrip title="Visually similar images" items={result.visually_similar_images} />
        </div>
      )}
    </div>
  );
}
