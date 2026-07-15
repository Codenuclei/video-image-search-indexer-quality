"use client";

import { useEffect, useId, useRef, useState } from "react";
import { apiClient, type Person } from "@/lib/api";
import { FaceThumb, Input } from "@/components/ui";
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
      <Input
        value={query}
        disabled={disabled || busy}
        placeholder={busy ? busyLabel : "Search existing names to merge…"}
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
      />
      {searching && !busy && (
        <p className="mt-1 text-xs text-muted-foreground">Searching…</p>
      )}
      {searchError && <p className="mt-1 text-xs text-destructive">{searchError}</p>}
      {showDropdown && (
        <ul
          id={listId}
          role="listbox"
          className="absolute z-20 mt-1 max-h-48 w-full overflow-y-auto rounded-md border border-border bg-card shadow-lg"
        >
          {results.length === 0 && !searching ? (
            <li className="px-3 py-2 text-sm text-muted-foreground">No matching names</li>
          ) : (
            results.map((person) => (
              <li key={person.id}>
                <button
                  type="button"
                  role="option"
                  className={cn(
                    "flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition-colors hover:bg-accent"
                  )}
                  onClick={() => selectPerson(person)}
                >
                  <FaceThumb faceId={person.representative_face_id} className="h-8 w-8 shrink-0 rounded-md" />
                  <span className="min-w-0 flex-1 truncate font-medium">{person.name}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {person.occurrence_count} app.
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
