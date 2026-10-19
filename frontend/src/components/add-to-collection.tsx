"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Bookmark, Check, Plus } from "lucide-react";
import {
  addCollectionItems,
  createCollection,
  formatApiError,
  getApiErrorStatus,
  isAbortError,
  listCollections,
  removeCollectionItem,
  savedFileIds as fetchSavedFileIds,
  type CollectionSummary,
} from "@/lib/api";
import { requestReauth, SESSION_RESTORED_EVENT } from "@/lib/app-session";
import { localGet, localSet } from "@/lib/local-store";
import { cn } from "@/lib/utils";
import { toastApiError } from "@/lib/toast-api-error";
import { toast } from "sonner";

function toastCollectionError(error: unknown, fallback: string) {
  if (isAbortError(error)) return;
  if (getApiErrorStatus(error) === 401) requestReauth();
  toastApiError(formatApiError(error, fallback));
}

const LAST_COLLECTION_KEY = "dfi_last_collection_id";
const POPOVER_WIDTH = 256; // w-64
const VIEWPORT_MARGIN = 8;
const GAP = 8;
const SAVED_HYDRATE_DEBOUNCE_MS = 40;
const SAVED_HYDRATE_BATCH = 200;

/** Session-level cache so Saved tint survives row remounts after the menu closes. */
const savedFileIds = new Set<string>();
/** Ids we have already asked the server about (saved or not). */
const checkedFileIds = new Set<string>();
const pendingCheckIds = new Set<string>();
const savedListeners = new Set<() => void>();
let hydrateTimer: ReturnType<typeof setTimeout> | null = null;
let hydrateInFlight = false;
/** Pause batch flushes after a 401 until SESSION_RESTORED (avoids a 401 loop). */
let hydratePausedForAuth = false;

function notifySavedListeners() {
  savedListeners.forEach((fn) => fn());
}

function rememberSaved(fileId: string, saved: boolean) {
  checkedFileIds.add(fileId);
  if (saved) savedFileIds.add(fileId);
  else savedFileIds.delete(fileId);
}

/** Drop one file's Saved tint (e.g. after removing it from a collection elsewhere). */
export function forgetSavedFileId(fileId: string) {
  savedFileIds.delete(fileId);
  checkedFileIds.add(fileId);
  notifySavedListeners();
}

/** Clear in-memory Saved tints on logout / account switch. */
export function clearSavedFileIdsCache() {
  savedFileIds.clear();
  checkedFileIds.clear();
  pendingCheckIds.clear();
  hydratePausedForAuth = false;
  if (hydrateTimer) {
    clearTimeout(hydrateTimer);
    hydrateTimer = null;
  }
}

function scheduleSavedFlush() {
  if (hydratePausedForAuth) return;
  if (hydrateTimer) clearTimeout(hydrateTimer);
  hydrateTimer = setTimeout(() => {
    hydrateTimer = null;
    void flushSavedChecks();
  }, SAVED_HYDRATE_DEBOUNCE_MS);
}

function queueSavedCheck(fileId: string) {
  const id = fileId.trim();
  if (!id || checkedFileIds.has(id) || pendingCheckIds.has(id)) return;
  pendingCheckIds.add(id);
  scheduleSavedFlush();
}

async function flushSavedChecks() {
  if (hydrateInFlight || hydratePausedForAuth) return;
  const batch = Array.from(pendingCheckIds).filter((id) => !checkedFileIds.has(id));
  pendingCheckIds.clear();
  if (batch.length === 0) return;

  hydrateInFlight = true;
  let hitAuth = false;
  try {
    for (let i = 0; i < batch.length; i += SAVED_HYDRATE_BATCH) {
      const chunk = batch.slice(i, i + SAVED_HYDRATE_BATCH);
      try {
        const result = await fetchSavedFileIds(chunk);
        const hit = new Set(result.saved_file_ids ?? []);
        for (const id of chunk) {
          checkedFileIds.add(id);
          if (hit.has(id)) savedFileIds.add(id);
        }
      } catch (error) {
        // Re-queue so SESSION_RESTORED can hydrate the Saved tint after cookie mint.
        for (const id of chunk) pendingCheckIds.add(id);
        if (getApiErrorStatus(error) === 401) {
          hitAuth = true;
          hydratePausedForAuth = true;
          requestReauth();
          break;
        }
      }
    }
    notifySavedListeners();
  } finally {
    hydrateInFlight = false;
    if (!hitAuth && pendingCheckIds.size > 0) {
      scheduleSavedFlush();
    }
  }
}

/** After silent/banner re-auth, retry any Saved-tint ids left pending from a 401. */
function onSessionRestoredHydrate() {
  hydratePausedForAuth = false;
  if (pendingCheckIds.size === 0) return;
  scheduleSavedFlush();
}

if (typeof window !== "undefined") {
  window.addEventListener(SESSION_RESTORED_EVENT, onSessionRestoredHydrate);
}

type FileRef = {
  drive_file_id: string;
  name: string;
  mime_type: string;
};

type PopoverCoords = { top: number; left: number };

function computePopoverPosition(anchor: DOMRect, popoverHeight: number): PopoverCoords {
  const spaceBelow = window.innerHeight - anchor.bottom - VIEWPORT_MARGIN;
  const spaceAbove = anchor.top - VIEWPORT_MARGIN;
  const openUp =
    spaceBelow < popoverHeight + GAP && spaceAbove > spaceBelow;

  let top = openUp
    ? anchor.top - GAP - popoverHeight
    : anchor.bottom + GAP;
  top = Math.max(
    VIEWPORT_MARGIN,
    Math.min(top, window.innerHeight - VIEWPORT_MARGIN - popoverHeight)
  );

  let left = anchor.left;
  left = Math.max(
    VIEWPORT_MARGIN,
    Math.min(left, window.innerWidth - VIEWPORT_MARGIN - POPOVER_WIDTH)
  );
  return { top, left };
}

export function AddToCollectionButton({
  file,
  query,
}: {
  file: FileRef;
  query?: string;
}) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [collections, setCollections] = useState<CollectionSummary[]>([]);
  const [membership, setMembership] = useState<Record<string, boolean>>({});
  const [knownSaved, setKnownSaved] = useState(() => savedFileIds.has(file.drive_file_id));
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [coords, setCoords] = useState<PopoverCoords>({ top: 0, left: 0 });
  const [mounted, setMounted] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setMounted(true);
  }, []);

  // Drop stale membership when the button is reused for another file.
  useEffect(() => {
    setMembership({});
    setCollections([]);
    setOpen(false);
    setKnownSaved(savedFileIds.has(file.drive_file_id));
  }, [file.drive_file_id]);

  // Batched first-load hydrator: register id, one fetch per tick for unchecked ids.
  useEffect(() => {
    const id = file.drive_file_id;
    const sync = () => setKnownSaved(savedFileIds.has(id));
    savedListeners.add(sync);
    queueSavedCheck(id);
    sync();
    return () => {
      savedListeners.delete(sync);
    };
  }, [file.drive_file_id]);

  useLayoutEffect(() => {
    if (!open || !buttonRef.current) return;
    const place = () => {
      const anchor = buttonRef.current!.getBoundingClientRect();
      const height = popupRef.current?.offsetHeight ?? 280;
      setCoords(computePopoverPosition(anchor, height));
    };
    place();
    // Re-measure after content settles (loading → list).
    const raf = requestAnimationFrame(place);
    return () => cancelAnimationFrame(raf);
  }, [open, loading, collections.length]);

  useEffect(() => {
    if (!open) return;

    const onDoc = (event: MouseEvent) => {
      const target = event.target as Node;
      if (popupRef.current?.contains(target) || buttonRef.current?.contains(target)) {
        return;
      }
      setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const onResize = () => setOpen(false);
    const onScroll = (event: Event) => {
      const target = event.target;
      if (target instanceof Node && popupRef.current?.contains(target)) {
        // Scrolling the collections list itself should not close the popover.
        return;
      }
      setOpen(false);
    };

    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", onResize);
    // Capture so scrolling an overflow list (e.g. reverse-face) still closes.
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onResize);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [open]);

  async function openPanel() {
    setOpen(true);
    setLoading(true);
    try {
      const listed = await listCollections(file.drive_file_id);
      const lastId = localGet(LAST_COLLECTION_KEY);
      const sorted = [...listed.mine].sort((a, b) => {
        if (a.id === lastId) return -1;
        if (b.id === lastId) return 1;
        return a.name.localeCompare(b.name);
      });
      setCollections(sorted);
      const memberMap: Record<string, boolean> = {};
      for (const row of sorted) {
        memberMap[row.id] = Boolean(row.contains_file);
      }
      setMembership(memberMap);
      const saved = Object.values(memberMap).some(Boolean);
      rememberSaved(file.drive_file_id, saved);
      setKnownSaved(saved);
    } catch (error) {
      toastCollectionError(error, "Could not load collections");
      setOpen(false);
    } finally {
      setLoading(false);
    }
  }

  async function toggle(collectionId: string, checked: boolean) {
    setBusyId(collectionId);
    try {
      if (checked) {
        await removeCollectionItem(collectionId, file.drive_file_id);
        setMembership((prev) => {
          const next = { ...prev, [collectionId]: false };
          const saved = Object.values(next).some(Boolean);
          rememberSaved(file.drive_file_id, saved);
          setKnownSaved(saved);
          return next;
        });
        setCollections((prev) =>
          prev.map((row) =>
            row.id === collectionId
              ? { ...row, item_count: Math.max(0, row.item_count - 1), contains_file: false }
              : row
          )
        );
      } else {
        await addCollectionItems(collectionId, [
          {
            drive_file_id: file.drive_file_id,
            name: file.name,
            mime_type: file.mime_type,
            query: query?.trim() || null,
          },
        ]);
        setMembership((prev) => {
          const next = { ...prev, [collectionId]: true };
          rememberSaved(file.drive_file_id, true);
          setKnownSaved(true);
          return next;
        });
        setCollections((prev) =>
          prev.map((row) =>
            row.id === collectionId
              ? { ...row, item_count: row.item_count + 1, contains_file: true }
              : row
          )
        );
        localSet(LAST_COLLECTION_KEY, collectionId);
      }
    } catch (error) {
      toastCollectionError(error, "Could not update collection");
    } finally {
      setBusyId(null);
    }
  }

  async function createAndAdd() {
    const name = newName.trim();
    if (!name || creating) return;
    setCreating(true);
    try {
      const created = await createCollection(name);
      await addCollectionItems(created.id, [
        {
          drive_file_id: file.drive_file_id,
          name: file.name,
          mime_type: file.mime_type,
          query: query?.trim() || null,
        },
      ]);
      localSet(LAST_COLLECTION_KEY, created.id);
      setCollections((prev) => [
        {
          id: created.id,
          name: created.name,
          owner_email: created.owner_email,
          created_at: created.created_at,
          item_count: 1,
          cover_drive_file_ids: [file.drive_file_id],
          contains_file: true,
        },
        ...prev,
      ]);
      setMembership((prev) => {
        const next = { ...prev, [created.id]: true };
        rememberSaved(file.drive_file_id, true);
        setKnownSaved(true);
        return next;
      });
      setNewName("");
      toast.success(`Saved to “${created.name}”`);
    } catch (error) {
      toastCollectionError(error, "Could not create collection");
    } finally {
      setCreating(false);
    }
  }

  const isSaved = knownSaved || Object.values(membership).some(Boolean);

  const popup =
    open && mounted
      ? createPortal(
          <div
            ref={popupRef}
            role="dialog"
            aria-label="Add to collection"
            style={{ top: coords.top, left: coords.left, width: POPOVER_WIDTH }}
            className="fixed z-[60] rounded-lg border border-border bg-card p-2 text-card-foreground shadow-lg"
          >
            <p className="px-1.5 pb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              Collections
            </p>
            {loading ? (
              <p className="px-1.5 py-3 text-xs text-muted-foreground">Loading…</p>
            ) : collections.length === 0 ? (
              <p className="px-1.5 py-2 text-xs text-muted-foreground">No collections yet.</p>
            ) : (
              <ul className="max-h-48 space-y-0.5 overflow-y-auto">
                {collections.map((row) => {
                  const checked = Boolean(membership[row.id]);
                  return (
                    <li key={row.id}>
                      <button
                        type="button"
                        disabled={busyId === row.id}
                        onClick={() => void toggle(row.id, checked)}
                        className="flex w-full items-center gap-2 rounded-md px-1.5 py-1.5 text-left text-xs hover:bg-muted/70 disabled:opacity-50"
                      >
                        <span
                          className={cn(
                            "inline-flex h-4 w-4 shrink-0 items-center justify-center rounded border",
                            checked
                              ? "border-amber-500 bg-amber-500 text-white"
                              : "border-border bg-background"
                          )}
                        >
                          {checked ? <Check size={10} aria-hidden /> : null}
                        </span>
                        <span className="min-w-0 flex-1 truncate font-medium">{row.name}</span>
                        <span className="shrink-0 tabular-nums text-muted-foreground">
                          {row.item_count}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}

            <div className="mt-2 flex gap-1 border-t border-border pt-2">
              <input
                type="text"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void createAndAdd();
                  }
                }}
                placeholder="New collection"
                maxLength={120}
                className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1 text-xs outline-none focus:ring-1 focus:ring-ring"
              />
              <button
                type="button"
                disabled={!newName.trim() || creating}
                onClick={() => void createAndAdd()}
                className="inline-flex shrink-0 items-center gap-1 rounded-md bg-primary px-2 py-1 text-xs font-medium text-primary-foreground disabled:opacity-50"
                aria-label="Create collection and save"
              >
                <Plus size={12} aria-hidden />
                Add
              </button>
            </div>
          </div>,
          document.body
        )
      : null;

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => (open ? setOpen(false) : void openPanel())}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
          open || isSaved
            ? "bg-amber-500/15 text-amber-800 dark:text-amber-200"
            : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"
        )}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label={isSaved ? "Saved to collection" : "Add to collection"}
      >
        <Bookmark
          size={14}
          className="shrink-0"
          aria-hidden
          fill={isSaved ? "currentColor" : "none"}
        />
        <span>{isSaved ? "Saved" : "Save"}</span>
      </button>
      {popup}
    </>
  );
}
