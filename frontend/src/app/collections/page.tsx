"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Bookmark, Plus, Trash2 } from "lucide-react";
import {
  createCollection,
  deleteCollection,
  driveFileThumbnailUrl,
  formatApiError,
  getApiErrorStatus,
  isAbortError,
  listCollections,
  removeCollectionView,
  type CollectionSummary,
  type SharedCollectionSummary,
} from "@/lib/api";
import { clearSavedFileIdsCache } from "@/components/add-to-collection";
import { Button, ConfirmDialog, Input, LoadingLabel } from "@/components/ui";
import { requestReauth, SESSION_RESTORED_EVENT } from "@/lib/app-session";
import { toastApiError } from "@/lib/toast-api-error";
import { toast } from "sonner";

function toastCollectionError(error: unknown, fallback: string) {
  if (isAbortError(error)) return;
  if (getApiErrorStatus(error) === 401) requestReauth();
  toastApiError(formatApiError(error, fallback));
}

function CoverGrid({ ids }: { ids: string[] }) {
  return (
    <div className="grid grid-cols-2 gap-0.5 bg-black/20">
      {([0, 1, 2, 3] as const).map((i) => {
        const fileId = ids[i];
        return (
          <div key={i} className="aspect-square overflow-hidden bg-muted/40">
            {fileId ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={driveFileThumbnailUrl(fileId)}
                alt=""
                className="h-full w-full object-cover"
              />
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

export default function CollectionsPage() {
  const [mine, setMine] = useState<CollectionSummary[]>([]);
  const [shared, setShared] = useState<SharedCollectionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [removeViewId, setRemoveViewId] = useState<string | null>(null);
  const [removingView, setRemovingView] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const listed = await listCollections();
      setMine(listed.mine);
      setShared(listed.shared);
    } catch (error) {
      if (getApiErrorStatus(error) === 401) requestReauth();
      const message = error instanceof Error ? error.message : "Could not load collections";
      setLoadError(message);
      setMine([]);
      setShared([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const onRestored = () => void load();
    window.addEventListener(SESSION_RESTORED_EVENT, onRestored);
    return () => window.removeEventListener(SESSION_RESTORED_EVENT, onRestored);
  }, [load]);

  async function onCreate() {
    const name = newName.trim();
    if (!name || creating) return;
    setCreating(true);
    try {
      await createCollection(name);
      setNewName("");
      await load();
      toast.success("Collection created");
    } catch (error) {
      toastCollectionError(error, "Could not create collection");
    } finally {
      setCreating(false);
    }
  }

  async function onConfirmDelete() {
    if (!deleteId || deleting) return;
    setDeleting(true);
    try {
      await deleteCollection(deleteId);
      // Files in the deleted collection may no longer be saved anywhere.
      clearSavedFileIdsCache();
      setDeleteId(null);
      await load();
      toast.success("Collection deleted");
    } catch (error) {
      toastCollectionError(error, "Could not delete collection");
    } finally {
      setDeleting(false);
    }
  }

  async function onConfirmRemoveView() {
    if (!removeViewId || removingView) return;
    setRemovingView(true);
    try {
      await removeCollectionView(removeViewId);
      setRemoveViewId(null);
      await load();
      toast.success("Removed from Shared with me");
    } catch (error) {
      toastCollectionError(error, "Could not remove");
    } finally {
      setRemovingView(false);
    }
  }

  const pendingDelete = mine.find((row) => row.id === deleteId);
  const pendingRemoveView = shared.find((row) => row.id === removeViewId);

  return (
    <div className="space-y-8">
      <div>
        <h2 className="text-xl font-semibold sm:text-2xl">Collections</h2>
        <p className="text-sm text-muted-foreground">
          Save pictures and videos from searches into named sets, then share a link with anyone on
          the team.
        </p>
      </div>

      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <Input
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void onCreate();
            }
          }}
          placeholder="New collection name"
          maxLength={120}
          className="sm:max-w-sm"
        />
        <Button
          onClick={() => void onCreate()}
          disabled={!newName.trim() || creating}
          className="inline-flex items-center gap-1.5"
        >
          <Plus size={14} aria-hidden />
          Create
        </Button>
      </div>

      {loading ? (
        <LoadingLabel>Loading collections…</LoadingLabel>
      ) : loadError ? (
        <div className="rounded-lg border border-border px-4 py-10 text-center">
          <p className="font-medium">Could not load collections</p>
          <p className="mt-1 text-sm text-muted-foreground">{loadError}</p>
          <Button className="mt-4" variant="secondary" onClick={() => void load()}>
            Retry
          </Button>
        </div>
      ) : (
        <>
          <section className="space-y-3">
            <h3 className="text-sm font-medium text-muted-foreground">My collections</h3>
            {mine.length === 0 ? (
              <div className="rounded-lg border border-dashed border-border px-4 py-10 text-center">
                <Bookmark className="mx-auto mb-2 text-muted-foreground" size={28} aria-hidden />
                <p className="text-sm text-muted-foreground">
                  No collections yet. Create one here, or save a search result with the bookmark
                  button.
                </p>
              </div>
            ) : (
              <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {mine.map((row) => (
                  <li
                    key={row.id}
                    className="overflow-hidden rounded-md border border-border bg-muted/30"
                  >
                    <Link href={`/collections/${row.id}`} className="block">
                      <CoverGrid ids={row.cover_drive_file_ids} />
                      <div className="flex items-start justify-between gap-2 px-3 py-3">
                        <div className="min-w-0">
                          <p className="truncate font-medium text-foreground">{row.name}</p>
                          <p className="text-xs text-muted-foreground">
                            {row.item_count} item{row.item_count === 1 ? "" : "s"}
                          </p>
                        </div>
                      </div>
                    </Link>
                    <div className="flex justify-end border-t border-border px-2 py-1.5">
                      <button
                        type="button"
                        onClick={() => setDeleteId(row.id)}
                        className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                        aria-label={`Delete ${row.name}`}
                      >
                        <Trash2 size={12} aria-hidden />
                        Delete
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="space-y-3">
            <h3 className="text-sm font-medium text-muted-foreground">Shared with me</h3>
            {shared.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                Collections others share with you will show up here when you open their link.
              </p>
            ) : (
              <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {shared.map((row) => (
                  <li
                    key={row.id}
                    className="overflow-hidden rounded-md border border-border bg-muted/30"
                  >
                    <Link href={`/collections/${row.id}`} className="block">
                      <CoverGrid ids={row.cover_drive_file_ids} />
                      <div className="px-3 py-3">
                        <p className="truncate font-medium text-foreground">{row.name}</p>
                        <p className="text-xs text-muted-foreground">
                          {row.item_count} item{row.item_count === 1 ? "" : "s"} · shared by{" "}
                          {row.owner_email}
                        </p>
                      </div>
                    </Link>
                    <div className="flex justify-end border-t border-border px-2 py-1.5">
                      <button
                        type="button"
                        onClick={() => setRemoveViewId(row.id)}
                        className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
                        aria-label={`Remove ${row.name} from Shared with me`}
                      >
                        <Trash2 size={12} aria-hidden />
                        Remove
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}

      <ConfirmDialog
        open={Boolean(deleteId)}
        title="Delete collection?"
        message={
          pendingDelete
            ? `Delete “${pendingDelete.name}” and all ${pendingDelete.item_count} saved items? This cannot be undone.`
            : "Delete this collection?"
        }
        confirmLabel={deleting ? "Deleting…" : "Delete"}
        variant="danger"
        onConfirm={() => void onConfirmDelete()}
        onCancel={() => setDeleteId(null)}
      />

      <ConfirmDialog
        open={Boolean(removeViewId)}
        title="Remove from Shared with me?"
        message={
          pendingRemoveView
            ? `Remove “${pendingRemoveView.name}” from your list? You can open the share link again later.`
            : "Remove this collection from your list?"
        }
        confirmLabel={removingView ? "Removing…" : "Remove"}
        variant="danger"
        onConfirm={() => void onConfirmRemoveView()}
        onCancel={() => setRemoveViewId(null)}
      />
    </div>
  );
}
