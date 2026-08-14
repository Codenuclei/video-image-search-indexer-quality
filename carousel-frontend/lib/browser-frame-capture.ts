/**
 * Capture a still from a playable video URL via hidden <video> + canvas.
 * Same-origin (or CORS-enabled) streams only — used by /test Frame picker.
 */

export type CapturedFrame = {
  frame_ts: number;
  dataUrl: string;
  blob: Blob;
  width: number;
  height: number;
};

export type CaptureOpts = {
  maxWidth?: number;
  mimeType?: string;
  quality?: number;
  /** Abort if seek/load takes longer than this (ms). Default 12s. */
  timeoutMs?: number;
};

const DEFAULT_MAX_WIDTH = 720;
const DEFAULT_MIME = "image/jpeg";
const DEFAULT_QUALITY = 0.85;
const DEFAULT_TIMEOUT_MS = 12_000;

function waitForEvent(
  el: HTMLMediaElement | HTMLVideoElement,
  event: string,
  timeoutMs: number
): Promise<void> {
  return new Promise((resolve, reject) => {
    const t = window.setTimeout(() => {
      cleanup();
      reject(new Error(`Timed out waiting for video "${event}"`));
    }, timeoutMs);
    const onOk = () => {
      cleanup();
      resolve();
    };
    const onErr = () => {
      cleanup();
      reject(new Error(`Video error while waiting for "${event}"`));
    };
    const cleanup = () => {
      window.clearTimeout(t);
      el.removeEventListener(event, onOk);
      el.removeEventListener("error", onErr);
    };
    el.addEventListener(event, onOk, { once: true });
    el.addEventListener("error", onErr, { once: true });
  });
}

async function ensureVideoReady(
  video: HTMLVideoElement,
  videoUrl: string,
  timeoutMs: number
): Promise<void> {
  if (video.src !== videoUrl) {
    video.src = videoUrl;
    video.load();
  }
  if (video.readyState >= 2 && video.videoWidth > 0) return;
  await waitForEvent(video, "loadeddata", timeoutMs);
}

async function seekVideo(
  video: HTMLVideoElement,
  tSec: number,
  timeoutMs: number
): Promise<void> {
  const target = Math.max(0, tSec);
  if (Number.isFinite(video.duration) && video.duration > 0) {
    video.currentTime = Math.min(target, Math.max(0, video.duration - 0.05));
  } else {
    video.currentTime = target;
  }
  if (video.seeking) {
    await waitForEvent(video, "seeked", timeoutMs);
  } else {
    // Some browsers fire seeked synchronously or skip when already at t.
    await new Promise<void>((r) => requestAnimationFrame(() => r()));
  }
}

function drawToCanvas(
  video: HTMLVideoElement,
  maxWidth: number
): HTMLCanvasElement {
  const srcW = video.videoWidth || 0;
  const srcH = video.videoHeight || 0;
  if (!srcW || !srcH) {
    throw new Error("Video has no dimensions yet");
  }
  const w = Math.min(maxWidth, srcW);
  const h = Math.round((srcH / srcW) * w);
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("Canvas 2D unavailable");
  ctx.drawImage(video, 0, 0, w, h);
  return canvas;
}

function canvasToBlob(
  canvas: HTMLCanvasElement,
  mimeType: string,
  quality: number
): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => {
        if (!blob) reject(new Error("canvas.toBlob failed (CORS?)"));
        else resolve(blob);
      },
      mimeType,
      quality
    );
  });
}

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("Failed to read frame blob"));
    reader.readAsDataURL(blob);
  });
}

/** Create (or reuse) a detached video element for seeking + canvas draw. */
function createCaptureVideo(): HTMLVideoElement {
  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.preload = "auto";
  // Same-origin proxy streams; anonymous helps when CDN CORS is set.
  video.crossOrigin = "anonymous";
  video.setAttribute("playsinline", "");
  video.style.cssText =
    "position:fixed;left:-9999px;top:0;width:1px;height:1px;opacity:0;pointer-events:none;";
  document.body.appendChild(video);
  return video;
}

/**
 * Seek `videoUrl` to `tSec` and return a JPEG/PNG still as blob + data URL.
 */
export async function captureVideoFrameAt(
  videoUrl: string,
  tSec: number,
  opts: CaptureOpts = {}
): Promise<CapturedFrame> {
  if (typeof document === "undefined") {
    throw new Error("captureVideoFrameAt requires a browser");
  }
  const maxWidth = opts.maxWidth ?? DEFAULT_MAX_WIDTH;
  const mimeType = opts.mimeType ?? DEFAULT_MIME;
  const quality = opts.quality ?? DEFAULT_QUALITY;
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  const video = createCaptureVideo();
  try {
    await ensureVideoReady(video, videoUrl, timeoutMs);
    await seekVideo(video, tSec, timeoutMs);
    const canvas = drawToCanvas(video, maxWidth);
    const blob = await canvasToBlob(canvas, mimeType, quality);
    const dataUrl = await blobToDataUrl(blob);
    return {
      frame_ts: tSec,
      dataUrl,
      blob,
      width: canvas.width,
      height: canvas.height,
    };
  } finally {
    video.pause();
    video.removeAttribute("src");
    video.load();
    video.remove();
  }
}

/**
 * Capture evenly spaced frames across `[startSec, endSec]` from one video load.
 */
export async function captureVideoFramesInRange(
  videoUrl: string,
  startSec: number,
  endSec: number,
  opts: CaptureOpts & { count?: number } = {}
): Promise<CapturedFrame[]> {
  if (typeof document === "undefined") {
    throw new Error("captureVideoFramesInRange requires a browser");
  }
  const count = Math.max(1, Math.min(opts.count ?? 12, 24));
  const maxWidth = opts.maxWidth ?? DEFAULT_MAX_WIDTH;
  const mimeType = opts.mimeType ?? DEFAULT_MIME;
  const quality = opts.quality ?? DEFAULT_QUALITY;
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  const lo = Math.max(0, startSec);
  const hi = Math.max(lo + 0.05, endSec);
  const span = hi - lo;

  const video = createCaptureVideo();
  const out: CapturedFrame[] = [];
  try {
    await ensureVideoReady(video, videoUrl, timeoutMs);
    for (let i = 0; i < count; i++) {
      const t =
        count === 1 ? lo : lo + (span * i) / (count - 1);
      await seekVideo(video, t, timeoutMs);
      const canvas = drawToCanvas(video, maxWidth);
      const blob = await canvasToBlob(canvas, mimeType, quality);
      const dataUrl = await blobToDataUrl(blob);
      out.push({
        frame_ts: Math.round(t * 100) / 100,
        dataUrl,
        blob,
        width: canvas.width,
        height: canvas.height,
      });
    }
    return out;
  } finally {
    video.pause();
    video.removeAttribute("src");
    video.load();
    video.remove();
  }
}
