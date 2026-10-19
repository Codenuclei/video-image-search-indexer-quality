"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, Check, Copy, Film, Link2, Pencil, Trash2, X } from "lucide-react";
import {
  driveFileDownloadUrl,
  driveFileThumbnailUrl,
  formatApiError,
  getApiErrorStatus,
  getCollection,
  isAbortError,
  removeCollectionItem,
  renameCollection,
  type Collection,
} from "@/lib/api";
import { forgetSavedFileId } from "@/components/add-to-collection";
import { Button, ConfirmDialog, DownloadButton, Input, LoadingLabel } from "@/components/ui";
import { requestReauth, SESSION_RESTORED_EVENT } from "@/lib/app-session";
import { toastApiError } from "@/lib/toast-api-error";
import { toast } from "sonner";

function toastCollectionError(error: unknown, fallback: string) {
  if (isAbortError(error)) return;
  if (getApiErrorStatus(error) === 401) requestReauth();
  toastApiError(formatApiError(error, fallback));
}

export default function CollectionDetailPage() {
  const params = useParams<{ id: string }>();
  const id = typeof params.id === "string" ? params.id : params.id?.[0] ?? "";
  const [collection, setCollection] = useState<Collection | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [savingName, setSavingName] = useState(false);
  const [copied, setCopied] = useState(false);
  const [removeFileId, setRemoveFileId] = useState<string | null>(null);
  const [removing, setRemoving] = useState(false);

  const load = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setNotFound(false);
    setLoadError(null);
    try {
      const row = await getCollection(id);
      setCollection(row);
      setNameDraft(row.name);
    } catch (error) {
      setCollection(null);
      if (getApiErrorStatus(error) === 401) requestReauth();
      const message = error instanceof Error ? error.message : "Could not load this collection";
      if (getApiErrorStatus(error) === 404) {
        setNotFound(true);
      } else {
        setLoadError(message);
      }
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const onRestored = () => void load();
    window.addEventListener(SESSION_RESTORED_EVENT, onRestored);
    return () => window.removeEventListener(SESSION_RESTORED_EVENT, onRestored);
  }, [load]);

  async function saveName() {
    if (!collection?.is_owner || !nameDraft.trim() || savingName) return;
    setSavingName(true);
    try {
      const updated = await renameCollection(collection.id, nameDraft.trim());
      setCollection(updated);
      setNameDraft(updated.name);
      setEditing(false);
      toast.success("Renamed");
    } catch (error) {
      toastCollectionError(error, "Could not rename");
    } finally {
      setSavingName(false);
    }
  }

  async function copyShareLink() {
    const url = `${window.location.origin}/collections/${id}`;
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      toast.success("Share link copied");
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error("Could not copy link");
    }
  }

  async function confirmRemove() {
    if (!collection?.is_owner || !removeFileId || removing) return;
    setRemoving(true);
    try {
      const updated = await removeCollectionItem(collection.id, removeFileId);
      // The file may still be in other collections; the next menu open re-learns that.
      forgetSavedFileId(removeFileId);
      setCollection(updated);
      setRemoveFileId(null);
    } catch (error) {
      toastCollectionError(error, "Could not remove item");
    } finally {
      setRemoving(false);
    }
  }

  if (loading) {
    return (
      <div className="space-y-4">
        <LoadingLabel>Loading collection…</LoadingLabel>
      </div>
    );
  }

  if (notFound) {
    return (
      <div className="space-y-4">
        <Link
          href="/collections"
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft size={14} aria-hidden />
          Back to collections
        </Link>
        <div className="rounded-lg border border-border px-4 py-10 text-center">
          <p className="font-medium">Collection not found</p>
          <p className="mt-1 text-sm text-muted-foreground">
            This link may be invalid, or the collection was deleted.
          </p>
        </div>
      </div>
    );
  }

  if (loadError || !collection) {
    return (
      <div className="space-y-4">
        <Link
          href="/collections"
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft size={14} aria-hidden />
          Back to collections
        </Link>
        <div className="rounded-lg border border-border px-4 py-10 text-center">
          <p className="font-medium">Could not load this collection</p>
          <p className="mt-1 text-sm text-muted-foreground">
            {loadError || "Something went wrong. Please try again."}
          </p>
          <Button className="mt-4" variant="secondary" onClick={() => void load()}>
            Retry
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <Link
        href="/collections"
        className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft size={14} aria-hidden />
        Back to collections
      </Link>

      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          {editing && collection.is_owner ? (
            <div className="flex flex-wrap items-center gap-2">
              <Input
                value={nameDraft}
                onChange={(e) => setNameDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void saveName();
                  }
                  if (e.key === "Escape") {
                    setEditing(false);
                    setNameDraft(collection.name);
                  }
                }}
                maxLength={120}
                className="max-w-md text-lg font-semibold"
                autoFocus
              />
              <Button onClick={() => void saveName()} disabled={savingName || !nameDraft.trim()}>
                <Check size={14} aria-hidden />
                Save
              </Button>
              <Button
                variant="secondary"
                onClick={() => {
                  setEditing(false);
                  setNameDraft(collection.name);
                }}
              >
                <X size={14} aria-hidden />
                Cancel
              </Button>
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="truncate text-xl font-semibold sm:text-2xl">{collection.name}</h2>
              {collection.is_owner && (
                <button
                  type="button"
                  onClick={() => setEditing(true)}
                  className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
                >
                  <Pencil size={12} aria-hidden />
                  Rename
                </button>
              )}
            </div>
          )}
          <p className="mt-1 text-sm text-muted-foreground">
            {collection.items.length} item{collection.items.length === 1 ? "" : "s"}
            {!collection.is_owner ? (
              <>
                {" "}
                · Shared by <span className="font-medium text-foreground">{collection.owner_email}</span>
              </>
            ) : null}
          </p>
        </div>

        {collection.is_owner && (
          <Button
            variant="secondary"
            onClick={() => void copyShareLink()}
            className="inline-flex shrink-0 items-center gap-1.5"
          >
            {copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />}
            {copied ? "Copied" : "Copy share link"}
          </Button>
        )}
        {!collection.is_owner && (
          <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
            <Link2 size={12} aria-hidden />
            Shared collection · view only
          </span>
        )}
      </div>

      {collection.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No items yet. Save files from Search with the bookmark button.
        </p>
      ) : (
        <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {collection.items.map((item) => {
            const isVideo = item.mime_type.startsWith("video/");
            return (
              <li
                key={item.drive_file_id}
                className="overflow-hidden rounded-md border border-border bg-muted/30"
              >
                <div className="relative aspect-[4/3] w-full overflow-hidden bg-black/20">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={driveFileThumbnailUrl(item.drive_file_id)}
                    alt={item.name}
                    className="h-full w-full object-cover"
                  />
                  {isVideo && (
                    <span className="absolute left-2 top-2 inline-flex items-center gap-1 rounded-md bg-black/70 px-1.5 py-0.5 text-[10px] font-medium text-white">
                      <Film size={10} aria-hidden />
                      Video
                    </span>
                  )}
                </div>
                <div className="flex flex-col gap-2 px-3 py-3 text-sm">
                  <p className="truncate font-medium" title={item.name}>
                    {item.name}
                  </p>
                  {item.query ? (
                    <span
                      className="inline-block max-w-full truncate rounded border border-violet-500/30 bg-violet-500/10 px-1.5 py-0.5 text-[10px] font-medium text-violet-700 dark:text-violet-300"
                      title={item.query}
                    >
                      {item.query}
                    </span>
                  ) : null}
                  <div className="flex flex-wrap items-center gap-2 pt-0.5">
                    <DownloadButton
                      url={driveFileDownloadUrl(item.drive_file_id)}
                      filename={item.name}
                      variant="primary"
                    />
                    {collection.is_owner && (
                      <button
                        type="button"
                        onClick={() => setRemoveFileId(item.drive_file_id)}
                        className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                      >
                        <Trash2 size={12} aria-hidden />
                        Remove
                      </button>
                    )}
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <ConfirmDialog
        open={Boolean(removeFileId)}
        title="Remove from collection?"
        message="This removes the item from the collection. The file in Drive is not deleted."
        confirmLabel={removing ? "Removing…" : "Remove"}
        variant="danger"
        onConfirm={() => void confirmRemove()}
        onCancel={() => setRemoveFileId(null)}
      />
    </div>
  );
}
