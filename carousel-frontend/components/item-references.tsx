"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ImagePlus, Link2, Trash2, Type, Upload } from "lucide-react";
import { toast } from "sonner";
import {
  apiAssetUrl,
  apiClient,
  formatApiError,
  type CarouselItemReference,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatTimestamp, uniquePickerFrames } from "@/app/carousel/utils";
import { FrameImg } from "@/components/frame-img";

type Props = {
  driveFileId: string;
  kind: "theme" | "hook";
  targetKey: string;
  targetLabel: string;
  /** Optional window so hooks can pick a frame from the same clip. */
  frameStartSec?: number;
  frameEndSec?: number | null;
  items?: CarouselItemReference[];
  onAdded?: (item: CarouselItemReference) => void;
  onRemoved?: (id: number) => void;
  className?: string;
};

function displayImageSrc(url: string | null | undefined): string | null {
  if (!url) return null;
  if (url.startsWith("http://") || url.startsWith("https://")) return url;
  return apiAssetUrl(url);
}

export function ItemReferences({
  driveFileId,
  kind,
  targetKey,
  targetLabel,
  frameStartSec,
  frameEndSec,
  items = [],
  onAdded,
  onRemoved,
  className,
}: Props) {
  const [open, setOpen] = useState(items.length > 0);
  const [tab, setTab] = useState<"image" | "copy">("image");
  const [imageUrl, setImageUrl] = useState("");
  const [copyText, setCopyText] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [pickingFrame, setPickingFrame] = useState(false);
  const [loadingFrames, setLoadingFrames] = useState(false);
  const [frameAttempt, setFrameAttempt] = useState(1);
  const [frameError, setFrameError] = useState<string | null>(null);
  const frameAbortRef = useRef<AbortController | null>(null);
  const [frameItems, setFrameItems] = useState<
    { text: string; frame_ts: number; preview_url: string }[]
  >([]);
  const [status, setStatus] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const statusTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (statusTimer.current) clearTimeout(statusTimer.current);
    frameAbortRef.current?.abort();
  }, []);

  useEffect(() => {
    if (items.length > 0) setOpen(true);
  }, [items.length, targetKey]);

  const imageRefs = useMemo(
    () => items.filter((r) => r.ref_kind === "image"),
    [items]
  );
  const copyRefs = useMemo(
    () => items.filter((r) => r.ref_kind === "copy"),
    [items]
  );
  const count = items.length;

  function announceAdded() {
    // Keep the panel open so the new chip is visible; make the confirmation obvious.
    setOpen(true);
    setStatus("Reference added");
    toast.success("Reference added");
    if (statusTimer.current) clearTimeout(statusTimer.current);
    statusTimer.current = setTimeout(() => setStatus(null), 2500);
  }

  async function addImage(url: string, frameTs?: number | null) {
    const trimmed = url.trim();
    if (!driveFileId || !targetKey || !trimmed) return;
    setSaving(true);
    setStatus(null);
    try {
      const res = await apiClient.carouselReferenceCreate({
        drive_file_id: driveFileId,
        target_kind: kind,
        target_key: targetKey,
        target_label: targetLabel,
        ref_kind: "image",
        image_url: trimmed,
        frame_ts: frameTs ?? null,
        note: note.trim() || undefined,
      });
      onAdded?.(res.item);
      setImageUrl("");
      setNote("");
      setPickingFrame(false);
      announceAdded();
    } catch (e) {
      setStatus(formatApiError(e, "Could not save image ref"));
    } finally {
      setSaving(false);
    }
  }

  async function addCopy() {
    const trimmed = copyText.trim();
    if (!driveFileId || !targetKey || !trimmed) return;
    setSaving(true);
    setStatus(null);
    try {
      const res = await apiClient.carouselReferenceCreate({
        drive_file_id: driveFileId,
        target_kind: kind,
        target_key: targetKey,
        target_label: targetLabel,
        ref_kind: "copy",
        copy_text: trimmed,
        note: note.trim() || undefined,
      });
      onAdded?.(res.item);
      setCopyText("");
      setNote("");
      announceAdded();
    } catch (e) {
      setStatus(formatApiError(e, "Could not save copy ref"));
    } finally {
      setSaving(false);
    }
  }

  async function remove(id: number) {
    setSaving(true);
    setStatus(null);
    try {
      await apiClient.carouselReferenceDelete(id);
      onRemoved?.(id);
      setStatus("Removed");
      setTimeout(() => setStatus(null), 1200);
    } catch (e) {
      setStatus(formatApiError(e, "Could not remove ref"));
    } finally {
      setSaving(false);
    }
  }

  async function loadFrames() {
    if (frameStartSec == null || !driveFileId) return;
    frameAbortRef.current?.abort();
    const ac = new AbortController();
    frameAbortRef.current = ac;
    setPickingFrame(true);
    setLoadingFrames(true);
    setFrameAttempt(1);
    setFrameError(null);
    setStatus(null);
    try {
      let loaded: { text: string; frame_ts: number; preview_url: string }[] = [];
      let preparing = false;
      for (let attempt = 1; attempt <= 3; attempt++) {
        setFrameAttempt(attempt);
        const res = await apiClient.carouselTranscriptFrames({
          driveFileId,
          startSec: Math.max(0, frameStartSec - 4),
          endSec: frameEndSec != null ? frameEndSec + 4 : frameStartSec + 28,
          limit: 16,
          waitMs: 45_000,
          timeoutMs: 60_000,
          silent: true,
          signal: ac.signal,
        });
        if (ac.signal.aborted) return;
        loaded = uniquePickerFrames(res.items ?? []);
        preparing = Boolean(res.quality?.preparing);
        if (loaded.length) break;
        if (attempt < 3) {
          await new Promise((resolve) => window.setTimeout(resolve, 1_200 * attempt));
        }
      }
      if (!loaded.length) {
        throw new Error(
          preparing
            ? "Frames are still being prepared. Please retry in a moment."
            : "No usable frames could be extracted from this video span."
        );
      }
      setFrameItems(loaded);
    } catch (e) {
      if (ac.signal.aborted) return;
      // Keep the picker open with a clear error + Retry instead of silently closing.
      setFrameItems([]);
      setFrameError(formatApiError(e, "Could not load frames"));
    } finally {
      // Only the latest request may clear the spinner (a cancelled one must not).
      if (frameAbortRef.current === ac) setLoadingFrames(false);
    }
  }

  function cancelFrames() {
    frameAbortRef.current?.abort();
    frameAbortRef.current = null;
    setLoadingFrames(false);
    setPickingFrame(false);
    setFrameError(null);
  }

  async function uploadFile(file: File | null | undefined) {
    if (!file || !driveFileId || !targetKey) return;
    setSaving(true);
    setStatus("Uploading…");
    try {
      const uploaded = await apiClient.carouselReferenceUploadImage(file);
      const url = (uploaded.url || "").trim();
      if (!url) throw new Error("Upload returned no URL");
      const res = await apiClient.carouselReferenceCreate({
        drive_file_id: driveFileId,
        target_kind: kind,
        target_key: targetKey,
        target_label: targetLabel,
        ref_kind: "image",
        image_url: url,
        note: note.trim() || file.name || undefined,
      });
      onAdded?.(res.item);
      setImageUrl("");
      setNote("");
      setPickingFrame(false);
      announceAdded();
    } catch (e) {
      setStatus(formatApiError(e, "Could not upload image"));
    } finally {
      setSaving(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  return (
    <div
      className={cn("item-refs", className)}
      onClick={(e) => e.stopPropagation()}
      onPointerDown={(e) => e.stopPropagation()}
      data-testid={`item-refs-${kind}`}
    >
      <div className="item-refs-row">
        <button
          type="button"
          className={cn("item-refs-toggle", open && "is-open")}
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          <Link2 size={11} strokeWidth={2.25} />
          {open ? "Hide refs" : count ? `Refs (${count})` : "Add ref"}
        </button>
        {status ? (
          <span
            className={cn("item-refs-note", status === "Reference added" && "is-success")}
            role="status"
          >
            {status}
          </span>
        ) : null}
      </div>

      {open ? (
        <div className="item-refs-panel">
          {(imageRefs.length > 0 || copyRefs.length > 0) && (
            <ul className="item-refs-list">
              {imageRefs.map((r) => {
                const src = displayImageSrc(r.image_url);
                return (
                  <li key={r.id} className="item-refs-chip is-image">
                    {src ? (
                      <FrameImg src={src} className="item-refs-thumb" />
                    ) : (
                      <span className="item-refs-thumb is-empty" />
                    )}
                    <span className="item-refs-chip-body">
                      <span className="item-refs-chip-kind">Image</span>
                      <span className="item-refs-chip-text" title={r.image_url ?? ""}>
                        {r.note ||
                          (r.frame_ts != null
                            ? `Frame @ ${formatTimestamp(r.frame_ts)}`
                            : r.image_url?.slice(0, 48) || "Image")}
                      </span>
                    </span>
                    <button
                      type="button"
                      className="item-refs-remove"
                      aria-label="Remove image reference"
                      disabled={saving}
                      onClick={() => void remove(r.id)}
                    >
                      <Trash2 size={12} />
                    </button>
                  </li>
                );
              })}
              {copyRefs.map((r) => (
                <li key={r.id} className="item-refs-chip is-copy">
                  <span className="item-refs-copy-icon" aria-hidden>
                    <Type size={12} />
                  </span>
                  <span className="item-refs-chip-body">
                    <span className="item-refs-chip-kind">
                      {r.note ? `Copy · ${r.note}` : "Copy"}
                    </span>
                    <span className="item-refs-chip-text">{r.copy_text}</span>
                  </span>
                  <button
                    type="button"
                    className="item-refs-remove"
                    aria-label="Remove copy reference"
                    disabled={saving}
                    onClick={() => void remove(r.id)}
                  >
                    <Trash2 size={12} />
                  </button>
                </li>
              ))}
            </ul>
          )}

          <div className="item-refs-tabs" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={tab === "image"}
              className={cn("item-refs-tab", tab === "image" && "is-on")}
              onClick={() => setTab("image")}
            >
              <ImagePlus size={11} />
              Image
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "copy"}
              className={cn("item-refs-tab", tab === "copy" && "is-on")}
              onClick={() => setTab("copy")}
            >
              <Type size={11} />
              Copy
            </button>
          </div>

          {tab === "image" ? (
            <div className="item-refs-form">
              <input
                type="url"
                className="item-refs-input"
                placeholder="Paste image URL or Drive file id…"
                value={imageUrl}
                disabled={saving}
                onChange={(e) => setImageUrl(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void addImage(imageUrl);
                  }
                }}
              />
              <input
                type="text"
                className="item-refs-input is-note"
                placeholder="Optional label"
                value={note}
                disabled={saving}
                maxLength={200}
                onChange={(e) => setNote(e.target.value)}
              />
              <div className="item-refs-actions">
                <button
                  type="button"
                  className="studio-btn studio-btn-ghost studio-btn-sm"
                  disabled={saving || !imageUrl.trim()}
                  onClick={() => void addImage(imageUrl)}
                >
                  Attach URL
                </button>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/jpeg,image/png,image/webp,image/gif,.jpg,.jpeg,.png,.webp,.gif"
                  className="item-refs-file-input"
                  disabled={saving}
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) void uploadFile(f);
                  }}
                />
                <button
                  type="button"
                  className="studio-btn studio-btn-ghost studio-btn-sm"
                  disabled={saving}
                  onClick={() => fileInputRef.current?.click()}
                >
                  <Upload size={11} strokeWidth={2.25} />
                  Upload file
                </button>
                {frameStartSec != null ? (
                  <button
                    type="button"
                    className="studio-btn studio-btn-ghost studio-btn-sm"
                    disabled={saving}
                    onClick={() => void loadFrames()}
                  >
                    Pick video frame
                  </button>
                ) : null}
              </div>
              {pickingFrame ? (
                <div className="item-refs-frames">
                  {loadingFrames ? (
                    <p className="item-refs-hint">
                      {frameAttempt > 1
                        ? `Still preparing frames — attempt ${frameAttempt} of 3.`
                        : "Preparing video frames…"}{" "}
                      <button type="button" className="item-refs-dismiss" onClick={cancelFrames}>
                        Cancel
                      </button>
                    </p>
                  ) : frameError ? (
                    <p className="item-refs-hint" role="alert">
                      {frameError}{" "}
                      <button
                        type="button"
                        className="item-refs-dismiss"
                        onClick={() => void loadFrames()}
                      >
                        Retry
                      </button>
                    </p>
                  ) : frameItems.length === 0 ? (
                    <p className="item-refs-hint">
                      No frames available here.{" "}
                      <button
                        type="button"
                        className="item-refs-dismiss"
                        onClick={() => void loadFrames()}
                      >
                        Retry
                      </button>
                    </p>
                  ) : (
                    <ul className="item-refs-frame-grid">
                      {frameItems.map((f) => (
                        <li key={`${f.frame_ts}-${f.preview_url}`}>
                          <button
                            type="button"
                            className="item-refs-frame-btn"
                            disabled={saving}
                            title={f.text}
                            onClick={() => void addImage(f.preview_url, f.frame_ts)}
                          >
                            <FrameImg src={apiAssetUrl(f.preview_url)} />
                            <span className="item-refs-frame-ts">{formatTimestamp(f.frame_ts)}</span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                  <button
                    type="button"
                    className="item-refs-dismiss"
                    onClick={cancelFrames}
                  >
                    Close frames
                  </button>
                </div>
              ) : null}
            </div>
          ) : (
            <div className="item-refs-form">
              <textarea
                className="item-refs-textarea"
                rows={3}
                placeholder={`Paste reference copy for this ${kind}…`}
                value={copyText}
                disabled={saving}
                maxLength={4000}
                spellCheck={false}
                onChange={(e) => setCopyText(e.target.value)}
              />
              <input
                type="text"
                className="item-refs-input is-note"
                placeholder="Optional label (e.g. competitor hook)"
                value={note}
                disabled={saving}
                maxLength={200}
                onChange={(e) => setNote(e.target.value)}
              />
              <div className="item-refs-actions">
                <button
                  type="button"
                  className="studio-btn studio-btn-ghost studio-btn-sm"
                  disabled={saving || !copyText.trim()}
                  onClick={() => void addCopy()}
                >
                  Attach copy
                </button>
              </div>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
