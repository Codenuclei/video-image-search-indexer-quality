"use client";

import { useEffect, useId, useRef, useState } from "react";
import { GitMerge, Search, UserRoundSearch } from "lucide-react";
import {
  apiClient,
  type LeadershipRoster,
  type Person,
} from "@/lib/api";
import {
  hydrateKeyFromDisk,
  readCache,
  withInFlight,
  writeCache,
} from "@/lib/data-cache";
import { getSearchSession } from "@/lib/search-session";
import { FaceThumb, LoadingLabel, Spinner } from "@/components/ui";
import { cn } from "@/lib/utils";

type PersonMergeSearchProps = {
  disabled?: boolean;
  busy?: boolean;
  busyLabel?: string;
  onSelect: (person: PersonMergeCandidate) => void;
};

export type PersonMergeCandidate = {
  source: "indexed" | "mu";
  id: number | null;
  name: string;
  role: string | null;
  representative_face_id: number | null;
  occurrence_count: number;
  file_count?: number | null;
  image_url?: string | null;
};

const LEADERSHIP_ROSTER_CACHE_KEY = "leadershipRoster:executive";

function filterPersonsByName(people: Person[], needle: string, limit = 20): Person[] {
  const q = needle.trim().toLowerCase();
  if (!q) return [];
  return people
    .filter((p) => p.name.trim().toLowerCase().includes(q))
    .sort((a, b) => a.name.localeCompare(b.name))
    .slice(0, limit);
}

/** Prefer API search; fall back to Indexed People list (cache / session / full list). */
async function resolveIndexedPersonMatches(query: string): Promise<Person[]> {
  const trimmed = query.trim();
  if (!trimmed) return [];

  let apiItems: Person[] | null = null;
  try {
    const items = await apiClient.searchPersons(trimmed);
    if (Array.isArray(items)) {
      apiItems = items;
      if (items.length > 0) return items;
    }
  } catch {
    /* fall through to local Indexed People */
  }

  const localPools: Person[][] = [];
  const sessionPersons = getSearchSession().persons;
  if (sessionPersons.length) localPools.push(sessionPersons);
  const cached = readCache<Person[]>("persons")?.data;
  if (cached?.length) localPools.push(cached);

  for (const pool of localPools) {
    const hits = filterPersonsByName(pool, trimmed);
    if (hits.length > 0) return hits;
  }

  try {
    const all = await apiClient.persons();
    const hits = filterPersonsByName(all, trimmed);
    if (hits.length > 0) return hits;
  } catch {
    /* ignore */
  }

  return apiItems ?? [];
}

async function leadershipRoster(): Promise<LeadershipRoster> {
  hydrateKeyFromDisk(LEADERSHIP_ROSTER_CACHE_KEY);
  const cached = readCache<LeadershipRoster>(LEADERSHIP_ROSTER_CACHE_KEY)?.data;
  if (cached) return cached;

  return withInFlight(LEADERSHIP_ROSTER_CACHE_KEY, async () => {
    const roster = await apiClient.leadershipRoster("executive");
    writeCache(
      LEADERSHIP_ROSTER_CACHE_KEY,
      roster,
      `${roster.section_id}:${roster.count}`,
      true
    );
    return roster;
  });
}

async function resolvePersonMatches(query: string): Promise<PersonMergeCandidate[]> {
  const trimmed = query.trim();
  const indexed = await resolveIndexedPersonMatches(trimmed);
  const candidates: PersonMergeCandidate[] = indexed.map((person) => ({
    source: "indexed",
    id: person.id,
    name: person.name,
    role: person.role,
    representative_face_id: person.representative_face_id,
    occurrence_count: person.occurrence_count,
    file_count: person.file_count,
  }));

  try {
    const roster = await leadershipRoster();
    const q = trimmed.toLowerCase();
    const indexedNames = new Set(indexed.map((person) => person.name.trim().toLowerCase()));
    for (const person of roster.people) {
      const normalizedName = person.name.trim().toLowerCase();
      if (!normalizedName.includes(q) || indexedNames.has(normalizedName)) continue;
      candidates.push({
        source: "mu",
        id: null,
        name: person.name,
        role: person.role || null,
        representative_face_id: null,
        occurrence_count: 0,
        file_count: 0,
        image_url: person.image_url,
      });
    }
  } catch {
    // Indexed People search remains usable if the live MU roster is unavailable.
  }

  return candidates.slice(0, 20);
}

export function PersonMergeSearch({
  disabled = false,
  busy = false,
  busyLabel = "Merging…",
  onSelect,
}: PersonMergeSearchProps) {
  const listId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const requestIdRef = useRef(0);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<PersonMergeCandidate[]>([]);
  const [searching, setSearching] = useState(false);
  const [open, setOpen] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  useEffect(() => {
    const trimmed = query.trim();
    if (trimmed.length < 1) {
      requestIdRef.current += 1;
      setResults([]);
      setSearching(false);
      setSearchError(null);
      return;
    }

    const requestId = ++requestIdRef.current;
    const timer = setTimeout(() => {
      setSearching(true);
      setSearchError(null);
      void resolvePersonMatches(trimmed)
        .then((items) => {
          if (requestId !== requestIdRef.current) return;
          setResults(items);
          setOpen(true);
          setSearchError(null);
        })
        .catch(() => {
          if (requestId !== requestIdRef.current) return;
          setResults([]);
          setSearchError("Could not search names");
        })
        .finally(() => {
          if (requestId === requestIdRef.current) setSearching(false);
        });
    }, 300);

    return () => clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    function handlePointerDown(event: MouseEvent) {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, []);

  function selectPerson(person: PersonMergeCandidate) {
    setQuery("");
    setResults([]);
    setOpen(false);
    onSelect(person);
  }

  const showDropdown = open && query.trim().length > 0 && !busy;

  return (
    <div ref={rootRef} className="relative min-w-[12rem] flex-1">
      <div className="relative">
        <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground">
          {busy ? (
            <Spinner size={15} className="text-amber-500" />
          ) : (
            <Search size={15} aria-hidden />
          )}
        </span>
        <input
          value={query}
          disabled={disabled || busy}
          placeholder={busy ? busyLabel : "Merge into existing person…"}
          aria-expanded={showDropdown}
          aria-controls={listId}
          aria-autocomplete="list"
          onFocus={() => {
            if (results.length > 0) setOpen(true);
          }}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
          }}
          className={cn(
            "w-full rounded-lg border border-input bg-background py-2 pl-9 pr-3 text-sm text-foreground outline-none transition-colors",
            "placeholder:text-muted-foreground focus:border-amber-400 focus:ring-2 focus:ring-amber-400/40",
            "disabled:opacity-60"
          )}
        />
      </div>
      {searching && !busy && (
        <p className="mt-1 text-xs text-muted-foreground">
          <LoadingLabel size={12}>Searching…</LoadingLabel>
        </p>
      )}
      {searchError && <p className="mt-1 text-xs text-destructive">{searchError}</p>}
      {showDropdown && (
        <ul
          id={listId}
          role="listbox"
          className="absolute z-20 mt-1.5 max-h-56 w-full overflow-y-auto rounded-xl border border-border bg-card p-1 shadow-xl ring-1 ring-black/5"
        >
          {results.length === 0 && !searching && !searchError ? (
            <li className="flex items-center gap-2 px-3 py-2.5 text-sm text-muted-foreground">
              <UserRoundSearch size={15} aria-hidden className="shrink-0" />
              No matching names
            </li>
          ) : (
            results.map((person) => (
              <li key={`${person.source}:${person.id ?? person.name}`}>
                <button
                  type="button"
                  role="option"
                  className="group flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-sm transition-colors hover:bg-amber-500/10"
                  onClick={() => selectPerson(person)}
                >
                  {person.source === "mu" && person.image_url ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={person.image_url}
                      alt=""
                      className="h-9 w-9 shrink-0 rounded-lg object-cover"
                    />
                  ) : (
                    <FaceThumb
                      faceId={person.representative_face_id}
                      className="h-9 w-9 shrink-0 rounded-lg"
                    />
                  )}
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{person.name}</span>
                    <span className="block text-xs text-muted-foreground">
                      {person.source === "mu"
                        ? `MU People${person.role ? ` · ${person.role}` : ""}`
                        : `${person.file_count ?? person.occurrence_count} file${
                            (person.file_count ?? person.occurrence_count) === 1 ? "" : "s"
                          }`}
                    </span>
                  </span>
                  <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-amber-500/15 px-2 py-1 text-[11px] font-semibold text-amber-700 opacity-0 transition-opacity group-hover:opacity-100 dark:text-amber-300">
                    <GitMerge size={12} aria-hidden />
                    Merge
                  </span>
                </button>
              </li>
            ))
          )}
        </ul>
      )}
    </div>
  );
}
