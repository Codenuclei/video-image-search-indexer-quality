"use client";

import { useEffect, useId, useRef, useState } from "react";
import { GitMerge, Search, UserRoundSearch } from "lucide-react";
import { apiClient, type Person } from "@/lib/api";
import { FaceThumb, LoadingLabel, Spinner } from "@/components/ui";
import { cn } from "@/lib/utils";

type PersonMergeSearchProps = {
  disabled?: boolean;
  busy?: boolean;
  busyLabel?: string;
  onSelect: (person: Person) => void;
};

export function PersonMergeSearch({
  disabled = false,
  busy = false,
  busyLabel = "Merging…",
  onSelect,
}: PersonMergeSearchProps) {
  const listId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Person[]>([]);
  const [searching, setSearching] = useState(false);
  const [open, setOpen] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  useEffect(() => {
    const trimmed = query.trim();
    if (trimmed.length < 1) {
      setResults([]);
      setSearching(false);
      setSearchError(null);
      return;
    }

    const timer = setTimeout(() => {
      setSearching(true);
      setSearchError(null);
      apiClient
        .searchPersons(trimmed)
        .then((items) => {
          setResults(items);
          setOpen(true);
        })
        .catch(() => {
          setResults([]);
          setSearchError("Could not search names");
        })
        .finally(() => setSearching(false));
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

  function selectPerson(person: Person) {
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
          {results.length === 0 && !searching ? (
            <li className="flex items-center gap-2 px-3 py-2.5 text-sm text-muted-foreground">
              <UserRoundSearch size={15} aria-hidden className="shrink-0" />
              No matching names
            </li>
          ) : (
            results.map((person) => (
              <li key={person.id}>
                <button
                  type="button"
                  role="option"
                  className="group flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-sm transition-colors hover:bg-amber-500/10"
                  onClick={() => selectPerson(person)}
                >
                  <FaceThumb faceId={person.representative_face_id} className="h-9 w-9 shrink-0 rounded-lg" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{person.name}</span>
                    <span className="block text-xs text-muted-foreground">
                      {person.occurrence_count} appearance{person.occurrence_count === 1 ? "" : "s"}
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
