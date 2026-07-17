"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Pencil, X } from "lucide-react";
import { apiClient, driveFilePreviewUrl, type FileFace } from "@/lib/api";
import { Button, Input, LoadingLabel } from "@/components/ui";
import { ModalOverlay } from "@/components/modal";
import { cn } from "@/lib/utils";

type ManualFaceTaggerProps = {
  driveFileId: string;
  mimeType: string;
  fileName: string;
};

type DragBox = { x0: number; y0: number; x1: number; y1: number };

function normalizeBox(box: DragBox) {
  const left = Math.min(box.x0, box.x1);
  const top = Math.min(box.y0, box.y1);
  const width = Math.abs(box.x1 - box.x0);
  const height = Math.abs(box.y1 - box.y0);
  return { left, top, width, height };
}

export function ManualFaceTagger({ driveFileId, mimeType, fileName }: ManualFaceTaggerProps) {
  const [open, setOpen] = useState(false);
  const [faceCount, setFaceCount] = useState<number | null>(null);

  useEffect(() => {
    apiClient
      .facesForFile(driveFileId)
      .then((items) => setFaceCount(items.length))
      .catch(() => setFaceCount(null));
  }, [driveFileId]);

  return (
    <div className="space-y-2">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={driveFilePreviewUrl(driveFileId, mimeType)}
        alt={fileName}
        className="w-full cursor-pointer rounded-lg border border-border object-cover"
        onClick={() => setOpen(true)}
      />
      <Button className="w-full" variant="secondary" onClick={() => setOpen(true)}>
        <span className="inline-flex items-center justify-center gap-1.5">
          <Pencil size={14} />
          Tag faces
          {faceCount != null ? ` (${faceCount})` : ""}
        </span>
      </Button>
      <p className="text-[10px] text-muted-foreground">
        Opens a large preview where you can draw boxes and name people.
      </p>

      <ManualFaceTagModal
        open={open}
        onClose={() => {
          setOpen(false);
          apiClient
            .facesForFile(driveFileId)
            .then((items) => setFaceCount(items.length))
            .catch(() => undefined);
        }}
        driveFileId={driveFileId}
        mimeType={mimeType}
        fileName={fileName}
      />
    </div>
  );
}

function ManualFaceTagModal({
  open,
  onClose,
  driveFileId,
  mimeType,
  fileName,
}: ManualFaceTaggerProps & { open: boolean; onClose: () => void }) {
  const overlayRef = useRef<HTMLDivElement>(null);
  const [faces, setFaces] = useState<FileFace[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const [drawMode, setDrawMode] = useState(true);
  const [drag, setDrag] = useState<DragBox | null>(null);
  const [draft, setDraft] = useState<{
    bbox_x: number;
    bbox_y: number;
    bbox_width: number;
    bbox_height: number;
  } | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    apiClient
      .facesForFile(driveFileId)
      .then(setFaces)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load faces"))
      .finally(() => setLoading(false));
  }, [driveFileId]);

  useEffect(() => {
    if (!open) return;
    setSelectedId(null);
    setName("");
    setNatural(null);
    setDraft(null);
    setDrag(null);
    setDrawMode(true);
    load();
  }, [open, driveFileId, load]);

  function selectFace(face: FileFace) {
    setDraft(null);
    setSelectedId(face.id);
    setName(face.person_name ?? "");
    setError(null);
  }

  function clientToImagePercent(clientX: number, clientY: number) {
    const el = overlayRef.current;
    if (!el) return { x: 0, y: 0 };
    const rect = el.getBoundingClientRect();
    const x = Math.min(100, Math.max(0, ((clientX - rect.left) / rect.width) * 100));
    const y = Math.min(100, Math.max(0, ((clientY - rect.top) / rect.height) * 100));
    return { x, y };
  }

  function onPointerDown(e: React.PointerEvent) {
    if (!drawMode || !natural) return;
    if ((e.target as HTMLElement).closest("[data-face-box]")) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    const p = clientToImagePercent(e.clientX, e.clientY);
    setDrag({ x0: p.x, y0: p.y, x1: p.x, y1: p.y });
    setSelectedId(null);
    setDraft(null);
    setError(null);
  }

  function onPointerMove(e: React.PointerEvent) {
    if (!drag) return;
    const p = clientToImagePercent(e.clientX, e.clientY);
    setDrag({ ...drag, x1: p.x, y1: p.y });
  }

  function onPointerUp() {
    if (!drag || !natural) {
      setDrag(null);
      return;
    }
    const box = normalizeBox(drag);
    setDrag(null);
    if (box.width < 1.2 || box.height < 1.2) return;
    setDraft({
      bbox_x: (box.left / 100) * natural.w,
      bbox_y: (box.top / 100) * natural.h,
      bbox_width: (box.width / 100) * natural.w,
      bbox_height: (box.height / 100) * natural.h,
    });
    setSelectedId(null);
    setName("");
  }

  async function saveTag() {
    if (!name.trim() || saving) return;
    setSaving(true);
    setError(null);
    try {
      if (draft) {
        const res = await apiClient.createManualFaceBox({
          drive_file_id: driveFileId,
          bbox_x: draft.bbox_x,
          bbox_y: draft.bbox_y,
          bbox_width: draft.bbox_width,
          bbox_height: draft.bbox_height,
          name: name.trim(),
        });
        setFaces((prev) => [...prev, res.face]);
        setSelectedId(res.face.id);
        setDraft(null);
        setName(res.person?.name ?? name.trim());
      } else if (selectedId) {
        const person = await apiClient.tagFace(selectedId, name.trim());
        setFaces((prev) =>
          prev.map((f) =>
            f.id === selectedId ? { ...f, person_id: person.id, person_name: person.name } : f
          )
        );
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Tag failed");
    } finally {
      setSaving(false);
    }
  }

  const selected = faces.find((f) => f.id === selectedId) ?? null;
  const liveBox = drag ? normalizeBox(drag) : null;
  const draftStyle =
    draft && natural
      ? {
          left: `${(draft.bbox_x / natural.w) * 100}%`,
          top: `${(draft.bbox_y / natural.h) * 100}%`,
          width: `${(draft.bbox_width / natural.w) * 100}%`,
          height: `${(draft.bbox_height / natural.h) * 100}%`,
        }
      : null;

  return (
    <ModalOverlay open={open} onClose={onClose} contentClassName="max-w-[min(96vw,72rem)]">
      <div className="overflow-hidden rounded-2xl border border-border bg-card shadow-2xl">
        {/* Header */}
        <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            <p className="text-[10px] font-semibold uppercase tracking-wide text-amber-600 dark:text-amber-400">
              Experimental · Manual face tag
            </p>
            <h2 className="truncate text-base font-semibold text-foreground">{fileName}</h2>
            <p className="text-xs text-muted-foreground">
              {drawMode
                ? "Drag on the image to draw a box, then name the person."
                : "Click an existing box to rename it."}
            </p>
          </div>
          <button
            type="button"
            aria-label="Close"
            onClick={onClose}
            className="rounded-lg p-2 text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <X size={18} />
          </button>
        </div>

        {/* Toolbar */}
        <div className="flex flex-wrap items-center gap-2 border-b border-border bg-muted/30 px-4 py-2">
          <button
            type="button"
            onClick={() => setDrawMode(true)}
            className={cn(
              "rounded-md px-3 py-1.5 text-xs font-medium",
              drawMode ? "bg-amber-500 text-black" : "border border-border bg-background text-muted-foreground"
            )}
          >
            Draw box
          </button>
          <button
            type="button"
            onClick={() => {
              setDrawMode(false);
              setDraft(null);
              setDrag(null);
            }}
            className={cn(
              "rounded-md px-3 py-1.5 text-xs font-medium",
              !drawMode ? "bg-sky-600 text-white" : "border border-border bg-background text-muted-foreground"
            )}
          >
            Select existing
          </button>
          <span className="text-xs text-muted-foreground">{faces.length} face(s)</span>
          {loading && (
            <span className="text-xs text-muted-foreground">
              <LoadingLabel size={12}>Loading…</LoadingLabel>
            </span>
          )}
        </div>

        {/* Large image canvas */}
        <div className="max-h-[min(70dvh,44rem)] overflow-auto bg-black/40 p-3 sm:p-4">
          <div
            ref={overlayRef}
            className={cn(
              "relative mx-auto w-fit max-w-full overflow-hidden rounded-lg border border-border bg-black",
              drawMode && "cursor-crosshair touch-none"
            )}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={() => setDrag(null)}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={driveFilePreviewUrl(driveFileId, mimeType)}
              alt={fileName}
              className="pointer-events-none block max-h-[min(65dvh,40rem)] w-auto max-w-full select-none object-contain"
              draggable={false}
              onLoad={(e) => {
                const img = e.currentTarget;
                setNatural({ w: img.naturalWidth, h: img.naturalHeight });
              }}
            />

            {natural &&
              faces.map((face) => {
                const left = (face.bbox_x / natural.w) * 100;
                const top = (face.bbox_y / natural.h) * 100;
                const width = (face.bbox_width / natural.w) * 100;
                const height = (face.bbox_height / natural.h) * 100;
                const active = face.id === selectedId;
                return (
                  <button
                    key={face.id}
                    type="button"
                    data-face-box
                    title={face.person_name ?? `Face #${face.id}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      selectFace(face);
                    }}
                    className={cn(
                      "absolute border-2 transition-colors",
                      active
                        ? "border-amber-400 bg-amber-400/20"
                        : face.person_name
                          ? "border-emerald-400/80 bg-emerald-400/10"
                          : face.detection_confidence >= 1
                            ? "border-violet-400/80 bg-violet-400/10"
                            : "border-sky-400/70 bg-sky-400/10 hover:border-sky-300"
                    )}
                    style={{ left: `${left}%`, top: `${top}%`, width: `${width}%`, height: `${height}%` }}
                  >
                    <span
                      className={cn(
                        "absolute -top-5 left-0 max-w-[10rem] truncate rounded px-1.5 text-[10px] font-medium",
                        active ? "bg-amber-400 text-black" : "bg-black/75 text-white"
                      )}
                    >
                      {face.person_name ?? (face.detection_confidence >= 1 ? "drawn" : `#${face.id}`)}
                    </span>
                  </button>
                );
              })}

            {liveBox && (
              <div
                className="pointer-events-none absolute border-2 border-dashed border-amber-300 bg-amber-300/20"
                style={{
                  left: `${liveBox.left}%`,
                  top: `${liveBox.top}%`,
                  width: `${liveBox.width}%`,
                  height: `${liveBox.height}%`,
                }}
              />
            )}

            {draftStyle && (
              <div
                className="pointer-events-none absolute border-2 border-amber-400 bg-amber-400/25"
                style={draftStyle}
              >
                <span className="absolute -top-5 left-0 rounded bg-amber-400 px-1.5 text-[10px] font-medium text-black">
                  new box
                </span>
              </div>
            )}
          </div>
        </div>

        {/* Footer naming bar */}
        <div className="space-y-2 border-t border-border px-4 py-3">
          {!loading && faces.length === 0 && !draft && (
            <p className="text-xs text-muted-foreground">
              No detected faces — use <span className="font-medium">Draw box</span> around the person.
            </p>
          )}

          {(selected || draft) && (
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
              <p className="shrink-0 text-xs text-muted-foreground sm:w-48">
                {draft
                  ? "New drawn box"
                  : `Face #${selected!.id}${
                      selected!.cluster_id != null ? ` · c${selected!.cluster_id}` : ""
                    }`}
              </p>
              <Input
                value={name}
                placeholder="Type a name…"
                disabled={saving}
                autoFocus
                className="flex-1"
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void saveTag();
                  }
                }}
              />
              <div className="flex gap-2">
                <Button onClick={() => void saveTag()} disabled={saving || !name.trim()}>
                  {saving ? <LoadingLabel>Saving…</LoadingLabel> : draft ? "Save box" : "Tag"}
                </Button>
                {draft && (
                  <Button
                    variant="secondary"
                    disabled={saving}
                    onClick={() => {
                      setDraft(null);
                      setName("");
                    }}
                  >
                    Cancel
                  </Button>
                )}
              </div>
            </div>
          )}

          {!selected && !draft && (
            <p className="text-xs text-muted-foreground">
              {drawMode ? "Drag on the image to start a box." : "Click a face box to edit its name."}
            </p>
          )}

          {error && <p className="text-xs text-destructive">{error}</p>}
        </div>
      </div>
    </ModalOverlay>
  );
}
