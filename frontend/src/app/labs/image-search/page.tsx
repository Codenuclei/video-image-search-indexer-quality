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
  type FaceReverseSearchResult,
  type OfficialImageSearchResult,
  type OfficialImageSearchStatus,
  type Person,
  type ReidStatus,
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

function RemoteImage({ url, alt }: { url: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <div className="flex aspect-square items-center justify-center rounded-lg border border-dashed border-border bg-muted/40 p-2 text-center text-[10px] text-muted-foreground">
        Image blocked
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
      className="aspect-square w-full rounded-lg border border-border bg-muted object-cover"
    />
  );
}

export default function OfficialImageSearchLabPage() {
  const [visionStatus, setVisionStatus] = useState<OfficialImageSearchStatus | null>(null);
  const [reidStatus, setReidStatus] = useState<ReidStatus | null>(null);
  const [personQuery, setPersonQuery] = useState("Pratham Mittal");
  const [people, setPeople] = useState<Person[]>([]);
  const [peopleOpen, setPeopleOpen] = useState(false);
  const [peopleSearching, setPeopleSearching] = useState(false);
  const [selectedPerson, setSelectedPerson] = useState<Person | null>(null);
  const [faceId, setFaceId] = useState("");
  const [lensResult, setLensResult] = useState<FaceReverseSearchResult | null>(null);
  const [visionResult, setVisionResult] = useState<OfficialImageSearchResult | null>(null);
  const [loadingStatus, setLoadingStatus] = useState(true);
  const [searching, setSearching] = useState(false);
  const [visionBusy, setVisionBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const peopleBoxRef = useRef<HTMLDivElement>(null);

  const loadStatus = useCallback(async () => {
    setLoadingStatus(true);
    try {
      const [vision, reid] = await Promise.all([
        apiClient.officialImageSearchStatus(),
        apiClient.reidStatus(),
      ]);
      setVisionStatus(vision);
      setReidStatus(reid);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load status");
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
    setLensResult(null);
    setVisionResult(null);
    if (person.representative_face_id) {
      setFaceId(String(person.representative_face_id));
    } else {
      setFaceId("");
      setError(`${person.name} has no representative face thumbnail yet`);
    }
  }

  async function runGoogleLens() {
    const id = Number(faceId);
    if (!Number.isFinite(id) || id <= 0) {
      setError("Pick an identified person first");
      return;
    }
    setSearching(true);
    setError(null);
    setVisionResult(null);
    try {
      setLensResult(await apiClient.reverseSearchFace(id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Google Lens reverse search failed");
    } finally {
      setSearching(false);
    }
  }

  async function runVisionFallback() {
    const id = Number(faceId);
    if (!Number.isFinite(id) || id <= 0) {
      setError("Pick an identified person first");
      return;
    }
    setVisionBusy(true);
    setError(null);
    try {
      setVisionResult(await apiClient.officialImageSearch({ face_id: id, max_results: 12 }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Cloud Vision search failed");
    } finally {
      setVisionBusy(false);
    }
  }

  const activeFaceId = Number(faceId);
  const preview = Number.isFinite(activeFaceId) && activeFaceId > 0 ? faceThumbnailUrl(activeFaceId) : null;
  const lensOpenUrl =
    Number.isFinite(activeFaceId) && activeFaceId > 0 ? apiClient.googleLensUrlForFace(activeFaceId) : null;
  const apifyReady = Boolean((reidStatus as ReidStatus & { apify_google_lens_configured?: boolean })?.apify_google_lens_configured);

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
            Google Lens Lab (Apify)
          </h2>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            Hidden lab — pick an identified person and run <strong>Apify Google Lens</strong> (AI Mode /
            exact matches). This is what browser Lens does — not Cloud Vision &quot;close-up&quot; guesses.
          </p>
        </div>
        <Button variant="secondary" disabled={loadingStatus || searching} onClick={loadStatus}>
          <span className="inline-flex items-center gap-1.5">
            <RefreshCw size={14} aria-hidden />
            Refresh status
          </span>
        </Button>
      </div>

      <Card className="space-y-2">
        <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
          <KeyRound size={16} className="text-amber-600" aria-hidden />
          Providers
          <span
            className={cn(
              "rounded-full px-2 py-0.5 text-[10px] font-semibold",
              apifyReady ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300" : "bg-amber-500/15 text-amber-700"
            )}
          >
            Apify Lens {apifyReady ? "ready" : "needs APIFY_TOKEN"}
          </span>
          {visionStatus && (
            <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] font-semibold text-muted-foreground">
              Vision {visionStatus.configured ? visionStatus.key_source : "off"} (fallback only)
            </span>
          )}
        </p>
        {!apifyReady && !loadingStatus && (
          <p className="text-xs text-amber-700 dark:text-amber-300">
            Add <code className="rounded bg-muted px-1">APIFY_TOKEN</code> to backend .env and Railway
            (from{" "}
            <ResultLink href="https://console.apify.com/account/integrations">Apify integrations</ResultLink>
            ). Actor default: <code className="rounded bg-muted px-1">borderline/google-lens</code>.
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

        {(selectedPerson || preview) && (
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-muted/30 p-3">
            {preview && (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={preview} alt="face" className="h-16 w-16 rounded-lg object-cover" />
            )}
            <div className="min-w-0 flex-1">
              <p className="font-semibold">{selectedPerson?.name ?? `Face ${faceId}`}</p>
              <p className="text-xs text-muted-foreground">Face {faceId || "—"}</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button disabled={searching || !faceId} onClick={runGoogleLens}>
                {searching ? <LoadingLabel>Running Lens…</LoadingLabel> : "Run Apify Google Lens"}
              </Button>
              {lensOpenUrl && (
                <a
                  href={lensOpenUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-secondary px-3 py-2 text-sm font-medium"
                >
                  Open in browser Lens
                  <ExternalLink size={12} aria-hidden />
                </a>
              )}
              <Button variant="secondary" disabled={visionBusy || !faceId} onClick={runVisionFallback}>
                {visionBusy ? <LoadingLabel>Vision…</LoadingLabel> : "Cloud Vision (weak)"}
              </Button>
            </div>
          </div>
        )}

        <label className="block space-y-1">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Face id</span>
          <Input value={faceId} onChange={(e) => setFaceId(e.target.value)} placeholder="26419" className="max-w-xs" />
        </label>
      </Card>

      {error && <ServiceErrorCard message={error} onRetry={runGoogleLens} onDismiss={() => setError(null)} />}

      {lensResult && (
        <div className="space-y-4">
          <Card className="space-y-3">
            <p className="text-xs text-muted-foreground">
              Provider <strong>{lensResult.provider}</strong> · face {lensResult.face_id} ·{" "}
              {lensResult.result_count} match(es)
            </p>
            {lensResult.google_guess && (
              <div>
                <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">AI / guess</p>
                <p className="mt-1 text-sm text-foreground whitespace-pre-wrap">{lensResult.google_guess}</p>
              </div>
            )}
            {lensResult.linkedin_url && (
              <p className="text-sm">
                LinkedIn: <ResultLink href={lensResult.linkedin_url}>{lensResult.linkedin_url}</ResultLink>
              </p>
            )}
          </Card>

          {lensResult.matches.length > 0 && (
            <Card className="space-y-3">
              <h3 className="text-sm font-semibold">Matches</h3>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {lensResult.matches.map((m, i) => (
                  <div key={`${m.url ?? i}`} className="rounded-lg border border-border bg-muted/20 p-3 space-y-2">
                    {m.thumbnail && <RemoteImage url={m.thumbnail} alt={m.title || "match"} />}
                    {m.url ? (
                      <ResultLink href={m.url}>{m.title || m.url}</ResultLink>
                    ) : (
                      <p className="text-sm">{m.title}</p>
                    )}
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>
      )}

      {visionResult && (
        <Card className="space-y-2">
          <h3 className="text-sm font-semibold">Cloud Vision fallback (usually weaker for people)</h3>
          <p className="text-xs text-muted-foreground">
            Best guess: {(visionResult.best_guess_labels || []).join(", ") || "—"} · similar images:{" "}
            {visionResult.visually_similar_images?.length ?? 0}
          </p>
          {visionResult.visually_similar_images?.length > 0 && (
            <div className="grid grid-cols-3 gap-2 sm:grid-cols-5">
              {visionResult.visually_similar_images.slice(0, 10).map((item) => (
                <a key={item.url} href={item.url} target="_blank" rel="noopener noreferrer">
                  <RemoteImage url={item.url} alt="similar" />
                </a>
              ))}
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
