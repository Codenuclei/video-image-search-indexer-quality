"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ExternalLink,
  Image as ImageIcon,
  KeyRound,
  RefreshCw,
  Search,
  UserRound,
} from "lucide-react";
import Link from "next/link";
import {
  apiClient,
  faceThumbnailUrl,
  type OfficialImageSearchResult,
  type OfficialImageSearchStatus,
  type Person,
} from "@/lib/api";
import { Button, Card, FaceThumb, Input, LoadingLabel, ServiceErrorCard } from "@/components/ui";
import { cn } from "@/lib/utils";

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

function RemoteImage({ url, alt }: { url: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <div className="flex aspect-square items-center justify-center rounded-lg border border-dashed border-border bg-muted/40 p-2 text-center text-[10px] text-muted-foreground">
        Image blocked / missing
      </div>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={url}
      alt={alt}
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
      className="aspect-square w-full rounded-lg border border-border bg-muted object-cover transition group-hover:opacity-80"
    />
  );
}

function ImageStrip({ title, items }: { title: string; items: { url: string; score?: number | null }[] }) {
  if (!items.length) return null;
  return (
    <Card className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        <span className="text-xs text-muted-foreground">{items.length}</span>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {items.slice(0, 20).map((item) => (
          <a
            key={item.url}
            href={item.url}
            target="_blank"
            rel="noopener noreferrer"
            className="group block space-y-1"
            title={item.url}
          >
            <RemoteImage url={item.url} alt={title} />
            <div className="flex items-center justify-between gap-1">
              <p className="truncate text-[10px] text-muted-foreground">{item.url.replace(/^https?:\/\//, "")}</p>
              <Score value={item.score} />
            </div>
          </a>
        ))}
      </div>
    </Card>
  );
}

export default function OfficialImageSearchLabPage() {
  const [status, setStatus] = useState<OfficialImageSearchStatus | null>(null);
  const [personQuery, setPersonQuery] = useState("");
  const [people, setPeople] = useState<Person[]>([]);
  const [peopleOpen, setPeopleOpen] = useState(false);
  const [peopleSearching, setPeopleSearching] = useState(false);
  const [selectedPerson, setSelectedPerson] = useState<Person | null>(null);
  const [faceId, setFaceId] = useState("");
  const [imageUrl, setImageUrl] = useState("");
  const [maxResults, setMaxResults] = useState("12");
  const [result, setResult] = useState<OfficialImageSearchResult | null>(null);
  const [loadingStatus, setLoadingStatus] = useState(true);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const peopleBoxRef = useRef<HTMLDivElement>(null);

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

  useEffect(() => {
    const q = personQuery.trim();
    if (q.length < 1) {
      setPeople([]);
      setPeopleSearching(false);
      return;
    }
    const timer = setTimeout(async () => {
      setPeopleSearching(true);
      try {
        setPeople(await apiClient.searchPersons(q, 12));
        setPeopleOpen(true);
      } catch {
        setPeople([]);
      } finally {
        setPeopleSearching(false);
      }
    }, 220);
    return () => clearTimeout(timer);
  }, [personQuery]);

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (!peopleBoxRef.current?.contains(e.target as Node)) setPeopleOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, []);

  function pickPerson(person: Person) {
    setSelectedPerson(person);
    setPersonQuery(person.name);
    setPeopleOpen(false);
    setImageUrl("");
    if (person.representative_face_id) {
      setFaceId(String(person.representative_face_id));
    } else {
      setFaceId("");
      setError(`${person.name} has no representative face thumbnail yet`);
    }
    setResult(null);
  }

  async function runSearch() {
    const trimmedUrl = imageUrl.trim();
    const id = Number(faceId);
    const limit = Number(maxResults);
    if (!trimmedUrl && (!Number.isFinite(id) || id <= 0)) {
      setError("Pick an identified person (or enter a face id / image URL)");
      return;
    }

    setSearching(true);
    setError(null);
    try {
      const payload = trimmedUrl
        ? { image_url: trimmedUrl, max_results: Number.isFinite(limit) ? limit : 12 }
        : { face_id: id, max_results: Number.isFinite(limit) ? limit : 12 };
      setResult(await apiClient.officialImageSearch(payload));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Official image search failed");
    } finally {
      setSearching(false);
    }
  }

  const activeFaceId = Number(faceId);
  const preview = imageUrl.trim() || (Number.isFinite(activeFaceId) && activeFaceId > 0 ? faceThumbnailUrl(activeFaceId) : null);
  const hasAnyResults =
    !!result &&
    (result.best_guess_labels.length > 0 ||
      result.web_entities.length > 0 ||
      result.full_matching_images.length > 0 ||
      result.partial_matching_images.length > 0 ||
      result.visually_similar_images.length > 0 ||
      result.pages_with_matching_images.length > 0);

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
            Hidden lab — search an <strong>identified person</strong>, run Google Cloud Vision reverse image
            search on their face thumbnail, and browse similar / matching images.
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
          Cloud Vision
          {status && (
            <span
              className={cn(
                "rounded-full px-2 py-0.5 text-[10px] font-semibold",
                status.configured ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300" : "bg-amber-500/15 text-amber-700"
              )}
            >
              {status.configured ? `ready · ${status.key_source}` : "not configured"}
            </span>
          )}
        </p>
        {loadingStatus ? (
          <p className="text-sm text-muted-foreground">
            <LoadingLabel size={16}>Checking key…</LoadingLabel>
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">
            Uses <code className="rounded bg-muted px-1">GOOGLE_VISION_API_KEY</code>. Old free reverse / Exa
            path is unchanged.
          </p>
        )}
      </Card>

      <Card className="space-y-4">
        <p className="flex items-center gap-2 text-sm font-medium">
          <UserRound size={16} className="text-sky-600" aria-hidden />
          Search identified people
        </p>

        <div ref={peopleBoxRef} className="relative">
          <Input
            value={personQuery}
            onChange={(e) => {
              setPersonQuery(e.target.value);
              setPeopleOpen(true);
            }}
            onFocus={() => personQuery.trim() && setPeopleOpen(true)}
            placeholder="Type a name — e.g. Pratham Mittal"
            autoComplete="off"
          />
          {peopleOpen && (peopleSearching || people.length > 0 || personQuery.trim().length > 0) && (
            <div className="absolute z-20 mt-1 max-h-72 w-full overflow-auto rounded-lg border border-border bg-card shadow-lg">
              {peopleSearching && (
                <p className="px-3 py-2 text-xs text-muted-foreground">
                  <LoadingLabel size={14}>Searching people…</LoadingLabel>
                </p>
              )}
              {!peopleSearching && people.length === 0 && personQuery.trim().length > 0 && (
                <p className="px-3 py-2 text-xs text-muted-foreground">No people matched</p>
              )}
              {people.map((person) => (
                <button
                  key={person.id}
                  type="button"
                  onClick={() => pickPerson(person)}
                  className="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-muted/60"
                >
                  <FaceThumb faceId={person.representative_face_id} className="h-10 w-10 rounded-md" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">{person.name}</span>
                    <span className="block text-[11px] text-muted-foreground">
                      {person.occurrence_count} appearances
                      {person.representative_face_id ? ` · face ${person.representative_face_id}` : " · no face"}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>

        {selectedPerson && (
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-muted/30 p-3">
            <FaceThumb faceId={selectedPerson.representative_face_id} className="h-16 w-16 rounded-lg" />
            <div className="min-w-0 flex-1">
              <p className="font-semibold">{selectedPerson.name}</p>
              <p className="text-xs text-muted-foreground">
                Face {selectedPerson.representative_face_id ?? "—"} · {selectedPerson.occurrence_count} appearances
              </p>
            </div>
            <Button disabled={searching || !selectedPerson.representative_face_id} onClick={runSearch}>
              {searching ? <LoadingLabel>Searching Vision…</LoadingLabel> : "Reverse search this person"}
            </Button>
          </div>
        )}

        <details className="rounded-lg border border-border bg-muted/20 p-3">
          <summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Advanced — face id / image URL
          </summary>
          <div className="mt-3 grid gap-3 lg:grid-cols-[1fr_1fr_auto]">
            <label className="space-y-1">
              <span className="text-xs text-muted-foreground">Face id</span>
              <Input value={faceId} onChange={(e) => setFaceId(e.target.value)} placeholder="470" />
            </label>
            <label className="space-y-1">
              <span className="text-xs text-muted-foreground">Public image URL</span>
              <Input value={imageUrl} onChange={(e) => setImageUrl(e.target.value)} placeholder="https://..." />
            </label>
            <label className="space-y-1">
              <span className="text-xs text-muted-foreground">Max</span>
              <Input value={maxResults} onChange={(e) => setMaxResults(e.target.value)} className="lg:w-20" />
            </label>
          </div>
          <div className="mt-3">
            <Button variant="secondary" disabled={searching} onClick={runSearch}>
              {searching ? <LoadingLabel>Searching…</LoadingLabel> : "Run with advanced input"}
            </Button>
          </div>
        </details>

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
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold">Vision results</h3>
              {selectedPerson && (
                <span className="rounded-full bg-sky-500/15 px-2 py-0.5 text-[10px] font-semibold text-sky-700 dark:text-sky-300">
                  {selectedPerson.name}
                </span>
              )}
              <span className="text-xs text-muted-foreground">
                {result.provider} · {result.key_source}
                {result.face_id != null && <> · face {result.face_id}</>}
              </span>
            </div>

            {!hasAnyResults && (
              <p className="text-sm text-muted-foreground">No web matches returned for this crop.</p>
            )}

            {result.best_guess_labels.length > 0 && (
              <div>
                <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Best guess</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {result.best_guess_labels.map((label) => (
                    <span
                      key={label}
                      className="rounded-full bg-sky-500/15 px-2.5 py-1 text-xs font-semibold text-sky-700 dark:text-sky-300"
                    >
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
                  {result.web_entities.slice(0, 24).map((entity, index) => (
                    <span
                      key={`${entity.entity_id ?? entity.description ?? index}`}
                      className="rounded-full border border-border bg-card px-2.5 py-1 text-xs"
                    >
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
                {result.pages_with_matching_images.slice(0, 12).map((page) => (
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
