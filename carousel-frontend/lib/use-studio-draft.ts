"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  isDraftStale,
  serializeStudioDraft,
  type StudioDraftRecord,
  type StudioDraftUpsertBody,
} from "@/lib/carousel-draft";

export type DraftSaveStatus = "idle" | "saving" | "saved" | "error";

export type DraftConflictState = {
  serverUpdatedAt: string;
  serverDraft: StudioDraftRecord;
} | null;

type UpsertResult = {
  id: number;
  drive_file_id: string;
  updated_at?: string | null;
  created_at?: string | null;
};

type UseStudioDraftAutosaveOpts = {
  driveFileId: string | null | undefined;
  /** When false, never schedule saves (e.g. during initial restore). */
  enabled: boolean;
  buildBody: () => Parameters<typeof serializeStudioDraft>[0] | null;
  upsert: (body: StudioDraftUpsertBody) => Promise<UpsertResult>;
  fetchDraft: (driveFileId: string) => Promise<StudioDraftRecord>;
  debounceMs?: number;
  /** Extra dependency key — bump when meaningful studio state changes. */
  revisionKey: string;
};

export function useStudioDraftAutosave({
  driveFileId,
  enabled,
  buildBody,
  upsert,
  fetchDraft,
  debounceMs = 1500,
  revisionKey,
}: UseStudioDraftAutosaveOpts) {
  const [status, setStatus] = useState<DraftSaveStatus>("idle");
  const [draftId, setDraftId] = useState<number | null>(null);
  const [conflict, setConflict] = useState<DraftConflictState>(null);
  const [lastSavedAt, setLastSavedAt] = useState<string | null>(null);

  /** Last client_updated_at we loaded or successfully wrote. */
  const localClientUpdatedAtRef = useRef<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const savingRef = useRef(false);
  const pausedRef = useRef(false);
  const buildBodyRef = useRef(buildBody);
  buildBodyRef.current = buildBody;
  const upsertRef = useRef(upsert);
  upsertRef.current = upsert;
  const fetchDraftRef = useRef(fetchDraft);
  fetchDraftRef.current = fetchDraft;

  const clearDraftIndicator = useCallback(() => {
    setDraftId(null);
    setStatus("idle");
    setLastSavedAt(null);
    setConflict(null);
    localClientUpdatedAtRef.current = null;
  }, []);

  const noteServerDraft = useCallback(
    (id: number, updatedAt: string | null | undefined, _clientUpdatedAt?: string | null) => {
      setDraftId(id);
      // Conflict baseline must use the server's updated_at clock. Preferring the
      // client's client_updated_at here caused false "server newer" banners on
      // every subsequent autosave (server now is always slightly later).
      if (updatedAt) {
        setLastSavedAt(updatedAt);
        localClientUpdatedAtRef.current = updatedAt;
      } else if (_clientUpdatedAt) {
        localClientUpdatedAtRef.current = _clientUpdatedAt;
      }
    },
    []
  );

  const pauseAutosave = useCallback((paused: boolean) => {
    pausedRef.current = paused;
  }, []);

  const flushSave = useCallback(
    async (opts?: { force?: boolean }) => {
      if (!driveFileId || !enabled || pausedRef.current) return;
      if (conflict && !opts?.force) return;
      if (savingRef.current) return;

      const raw = buildBodyRef.current();
      if (!raw || !String(raw.drive_file_id || "").trim()) return;

      if (!opts?.force) {
        try {
          const server = await fetchDraftRef.current(driveFileId);
          const serverAt = server.updated_at || server.created_at || null;
          const localAt = localClientUpdatedAtRef.current;
          // Only conflict when we already have a baseline and the server draft
          // moved forward (another tab/device wrote). Equal timestamps are fine.
          if (localAt && serverAt && isDraftStale(localAt, serverAt)) {
            setConflict({ serverUpdatedAt: serverAt, serverDraft: server });
            return;
          }
          if (server.id) setDraftId(server.id);
        } catch {
          // 404 / network: create or overwrite.
        }
      }

      const clientUpdatedAt = new Date().toISOString();
      const body = serializeStudioDraft({
        ...raw,
        drive_file_id: driveFileId,
        client_updated_at: clientUpdatedAt,
      });

      savingRef.current = true;
      setStatus("saving");
      try {
        const res = await upsertRef.current(body);
        setDraftId(res.id);
        // Align the local baseline to the server clock so the next preflight
        // GET does not treat our own write as a foreign newer draft.
        const updated = res.updated_at || res.created_at || clientUpdatedAt;
        localClientUpdatedAtRef.current = updated;
        setLastSavedAt(updated);
        setStatus("saved");
        setConflict(null);
      } catch {
        setStatus("error");
      } finally {
        savingRef.current = false;
      }
    },
    [conflict, driveFileId, enabled]
  );

  const keepLocal = useCallback(() => {
    setConflict(null);
    void flushSave({ force: true });
  }, [flushSave]);

  const scheduleSave = useCallback(() => {
    if (!driveFileId || !enabled || pausedRef.current || conflict) return;
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      void flushSave();
    }, debounceMs);
  }, [conflict, debounceMs, driveFileId, enabled, flushSave]);

  useEffect(() => {
    if (!driveFileId || !enabled || !revisionKey || conflict) return;
    scheduleSave();
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [conflict, driveFileId, enabled, revisionKey, scheduleSave]);

  useEffect(() => {
    if (!driveFileId || !enabled) return;
    const flushNow = () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      void flushSave();
    };
    const onHide = () => {
      if (document.visibilityState === "hidden") flushNow();
    };
    document.addEventListener("visibilitychange", onHide);
    window.addEventListener("pagehide", flushNow);
    return () => {
      document.removeEventListener("visibilitychange", onHide);
      window.removeEventListener("pagehide", flushNow);
    };
  }, [driveFileId, enabled, flushSave]);

  const statusLabel =
    status === "saving"
      ? "Saving…"
      : status === "saved"
        ? "Saved just now"
        : status === "error"
          ? "Save failed"
          : null;

  return {
    status,
    statusLabel,
    draftId,
    conflict,
    lastSavedAt,
    clearDraftIndicator,
    noteServerDraft,
    pauseAutosave,
    keepLocal,
    setConflict,
    flushSave,
  };
}
