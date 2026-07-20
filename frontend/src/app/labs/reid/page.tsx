"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ArrowLeft,
  BoxSelect,
  GitMerge,
  RefreshCw,
  ScanLine,
  Shirt,
  Sparkles,
  UserRound,
} from "lucide-react";
import Link from "next/link";
import {
  API_BASE,
  apiClient,
  type ReidGalleryItem,
  type ReidProveResult,
  type ReidStatus,
} from "@/lib/api";
import { Button, Card, FaceThumb, Input, LoadingLabel, ServiceErrorCard } from "@/components/ui";
import { cn } from "@/lib/utils";

function BodyCrop({ faceId, className }: { faceId: number; className?: string }) {
  const src = apiClient.reidBodyCropUrl(faceId);
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <div
        className={cn(
          "flex items-center justify-center rounded-lg border border-dashed border-border bg-muted/40 text-xs text-muted-foreground",
          className
        )}
      >
        No crop
      </div>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt="body crop"
      onError={() => setFailed(true)}
      className={cn("rounded-lg object-cover ring-1 ring-amber-400/40", className)}
    />
  );
}

function GalleryCard({ item }: { item: ReidGalleryItem }) {
  const cand = item.candidate;
  return (
    <Card className="overflow-hidden p-0">
      <div className="grid grid-cols-2 gap-px bg-border">
        <div className="relative bg-muted/30 p-2">
          <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Face</p>
          <FaceThumb faceId={item.face_id} className="mx-auto h-28 w-28 rounded-lg" />
        </div>
        <div className="relative bg-muted/30 p-2">
          <p className="mb-1.5 flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-300">
            <Shirt size={11} aria-hidden />
            YOLO body
          </p>
          <BodyCrop faceId={item.face_id} className="mx-auto h-28 w-full max-w-[8rem]" />
        </div>
      </div>
      {item.has_proof && item.proof_url && (
        <div className="border-t border-border bg-black/5 p-2">
          <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-emerald-700 dark:text-emerald-300">
            Proved boxes
          </p>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={`${API_BASE}${item.proof_url}`}
            alt="YOLO person boxes"
            className="max-h-48 w-full rounded-md object-contain"
          />
        </div>
      )}
      <div className="space-y-2 p-3">
        <div className="min-w-0">
          {item.person_name ? (
            <p className="flex items-center gap-1.5 font-semibold text-foreground">
              <UserRound size={14} className="shrink-0 text-sky-500" aria-hidden />
              {item.person_name}
            </p>
          ) : cand?.person_name ? (
            <p className="flex flex-col gap-0.5 font-semibold text-amber-800 dark:text-amber-200">
              <span className="inline-flex items-center gap-1.5">
                <GitMerge size={14} className="shrink-0" aria-hidden />
                Likely {cand.person_name}
                <span className="text-xs font-medium text-muted-foreground">
                  ({Math.round((cand.combined_score ?? 0) * 100)}%)
                </span>
              </span>
              <span className="text-[10px] font-medium text-muted-foreground">
                head {Math.round((cand.face_similarity ?? 0) * 100)}% · body{" "}
                {Math.round((cand.body_similarity ?? 0) * 100)}%
                {cand.gated_by_face ? " · face-gated" : ""}
              </span>
            </p>
          ) : (
            <p className="text-sm text-muted-foreground">Unlabeled</p>
          )}
          <p className="mt-0.5 truncate text-xs text-muted-foreground" title={item.file_path}>
            {item.file_name}
          </p>
        </div>
        <div className="flex flex-wrap gap-1">
          {item.is_full_body && (
            <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold text-emerald-700 dark:text-emerald-300">
              Full body
            </span>
          )}
          <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium text-muted-foreground">
            Area {item.prominence_pct}%
          </span>
          <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium text-muted-foreground">
            Height cov {item.body_coverage_pct}%
          </span>
        </div>
      </div>
    </Card>
  );
}

export default function ReidLabPage() {
  const [status, setStatus] = useState<ReidStatus | null>(null);
  const [items, setItems] = useState<ReidGalleryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState(false);
  const [proving, setProving] = useState(false);
  const [mediaId, setMediaId] = useState("4960");
  const [proveResult, setProveResult] = useState<ReidProveResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [st, gallery] = await Promise.all([apiClient.reidStatus(), apiClient.reidGallery(48)]);
      setStatus(st);
      setItems(gallery);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load lab data");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function scan() {
    setScanning(true);
    setError(null);
    try {
      await apiClient.reidBackfill(50);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Backfill failed");
    } finally {
      setScanning(false);
    }
  }

  async function proveOne() {
    const id = Number(mediaId);
    if (!Number.isFinite(id) || id <= 0) {
      setError("Enter a valid media id");
      return;
    }
    setProving(true);
    setError(null);
    try {
      const result = await apiClient.reidProve(id, true);
      setProveResult(result);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Prove failed");
    } finally {
      setProving(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <Link
            href="/"
            className="mb-2 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft size={12} aria-hidden />
            Back
          </Link>
          <h2 className="flex items-center gap-2 text-xl font-semibold sm:text-2xl">
            <Sparkles size={20} className="text-amber-500" aria-hidden />
            Body ID Lab
          </h2>
          <p className="mt-1 max-w-xl text-sm text-muted-foreground">
            Experimental — <strong>YOLOv8n person boxes</strong>, then Gemini body embed.
            &quot;Likely&quot; names require <strong>face agreement</strong> (same dress alone is rejected).
            Green = person, amber = face.
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <Button variant="secondary" disabled={loading || scanning || proving} onClick={() => load()}>
            <span className="inline-flex items-center gap-1.5">
              <RefreshCw size={14} aria-hidden />
              Refresh
            </span>
          </Button>
          <Button disabled={scanning || proving} onClick={scan}>
            <span className="inline-flex items-center gap-1.5">
              {scanning ? <LoadingLabel>Scanning…</LoadingLabel> : <ScanLine size={14} aria-hidden />}
              {!scanning && "Scan 50"}
            </span>
          </Button>
        </div>
      </div>

      <Card className="space-y-3">
        <p className="flex items-center gap-2 text-sm font-medium">
          <BoxSelect size={16} className="text-emerald-600" aria-hidden />
          Prove one media (draw bounding boxes)
        </p>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <Input
            value={mediaId}
            onChange={(e) => setMediaId(e.target.value)}
            placeholder="media id e.g. 4960"
            className="sm:max-w-[12rem]"
          />
          <Button disabled={proving} onClick={proveOne}>
            {proving ? <LoadingLabel>Detecting…</LoadingLabel> : "Run YOLO prove"}
          </Button>
        </div>
        {proveResult && (
          <div className="space-y-2">
            <p className="text-xs text-muted-foreground">
              {proveResult.file_name} · detector <strong>{proveResult.detector}</strong> ·{" "}
              {proveResult.persons_detected} person box(es) · {proveResult.faces_linked_to_person_box}{" "}
              face(s) linked · {proveResult.embedded} Gemini embed(s)
            </p>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={`${apiClient.reidProofUrl(proveResult.media_id)}?t=${Date.now()}`}
              alt="YOLO proof with bounding boxes"
              className="max-h-[28rem] w-full rounded-lg border border-border object-contain bg-muted/30"
            />
          </div>
        )}
      </Card>

      {status && (
        <div className="flex flex-wrap gap-2 text-xs">
          <span className="rounded-full border border-border bg-card px-3 py-1">
            Detector: <strong>{status.person_detector ?? "?"}</strong>
          </span>
          <span className="rounded-full border border-border bg-card px-3 py-1">
            Signatures: <strong>{status.body_signatures.total}</strong>
          </span>
          <span className="rounded-full border border-border bg-card px-3 py-1">
            Labeled: <strong>{status.body_signatures.labeled}</strong>
          </span>
          <span className="rounded-full border border-border bg-card px-3 py-1">
            Full body: <strong>{status.body_signatures.full_body}</strong>
          </span>
        </div>
      )}

      {error && <ServiceErrorCard message={error} onRetry={load} onDismiss={() => setError(null)} />}

      {loading && (
        <p className="text-sm text-muted-foreground">
          <LoadingLabel size={16}>Loading gallery…</LoadingLabel>
        </p>
      )}

      {!loading && items.length === 0 && !error && !proveResult && (
        <Card>
          <p className="text-sm text-muted-foreground">
            No body signatures yet. Enter a media id above and hit <strong>Run YOLO prove</strong> to
            see real person bounding boxes, then Gemini-index the crops.
          </p>
        </Card>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {items.map((item) => (
          <GalleryCard key={item.signature_id} item={item} />
        ))}
      </div>
    </div>
  );
}
