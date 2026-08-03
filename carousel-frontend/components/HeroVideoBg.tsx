"use client";

import { useEffect, useRef, useState } from "react";

const VIDEO_SRC =
  "https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260715_090628_7052d8a6-a094-4341-a4a2-ad58493a67a9.mp4";

const MAX_CAPTURE_WIDTH = 960;
const FRAME_INTERVAL = 1000 / 30;

type VideoWithFrameCallback = HTMLVideoElement & {
  requestVideoFrameCallback?: (
    callback: (now: number, metadata: { mediaTime: number }) => void,
  ) => number;
  cancelVideoFrameCallback?: (handle: number) => void;
};

/** Ping-pong canvas background — keeps the atmospheric motion treatment. */
export default function HeroVideoBg() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const displayCanvasRef = useRef<HTMLCanvasElement>(null);
  const framesRef = useRef<HTMLCanvasElement[]>([]);
  const lastTimeRef = useRef<number>(-1);
  const captureHandleRef = useRef<number | null>(null);
  const pingPongRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const directionRef = useRef<1 | -1>(1);
  const frameIndexRef = useRef(0);
  const capturingRef = useRef(true);
  const usingRvfRef = useRef(false);

  const [ready, setReady] = useState(false);

  useEffect(() => {
    const video = videoRef.current as VideoWithFrameCallback | null;
    if (!video) return;

    let cancelled = false;

    const stopCaptureLoop = () => {
      if (captureHandleRef.current == null) return;
      if (usingRvfRef.current && video.cancelVideoFrameCallback) {
        video.cancelVideoFrameCallback(captureHandleRef.current);
      } else {
        cancelAnimationFrame(captureHandleRef.current);
      }
      captureHandleRef.current = null;
    };

    const captureFrame = () => {
      if (cancelled || !capturingRef.current) return;
      if (video.readyState < 2) return;

      const t = video.currentTime;
      if (t === lastTimeRef.current) return;
      lastTimeRef.current = t;

      const srcW = video.videoWidth || 0;
      const srcH = video.videoHeight || 0;
      if (!srcW || !srcH) return;

      const w = Math.min(MAX_CAPTURE_WIDTH, srcW);
      const h = Math.round((srcH / srcW) * w);

      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.drawImage(video, 0, 0, w, h);
      framesRef.current.push(canvas);
    };

    const scheduleCapture = () => {
      if (cancelled || !capturingRef.current) return;

      if (typeof video.requestVideoFrameCallback === "function") {
        usingRvfRef.current = true;
        captureHandleRef.current = video.requestVideoFrameCallback(() => {
          captureFrame();
          scheduleCapture();
        });
      } else {
        usingRvfRef.current = false;
        captureHandleRef.current = requestAnimationFrame(() => {
          captureFrame();
          scheduleCapture();
        });
      }
    };

    const onEnded = () => {
      capturingRef.current = false;
      stopCaptureLoop();
      if (framesRef.current.length > 0) {
        setReady(true);
      }
    };

    const onLoaded = () => {
      video.muted = true;
      const playPromise = video.play();
      if (playPromise?.catch) {
        playPromise.catch(() => {});
      }
      scheduleCapture();
    };

    video.addEventListener("ended", onEnded);
    if (video.readyState >= 2) {
      onLoaded();
    } else {
      video.addEventListener("loadeddata", onLoaded, { once: true });
    }

    return () => {
      cancelled = true;
      capturingRef.current = false;
      video.removeEventListener("ended", onEnded);
      stopCaptureLoop();
      if (pingPongRef.current != null) {
        clearInterval(pingPongRef.current);
        pingPongRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (!ready) return;

    const canvas = displayCanvasRef.current;
    const frames = framesRef.current;
    if (!canvas || frames.length === 0) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const first = frames[0];
    canvas.width = first.width;
    canvas.height = first.height;
    frameIndexRef.current = 0;
    directionRef.current = 1;

    const draw = () => {
      const frame = frames[frameIndexRef.current];
      if (frame) {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(frame, 0, 0);
      }

      let next = frameIndexRef.current + directionRef.current;
      if (next >= frames.length - 1) {
        next = frames.length - 1;
        directionRef.current = -1;
      } else if (next <= 0) {
        next = 0;
        directionRef.current = 1;
      }
      frameIndexRef.current = next;
    };

    draw();
    pingPongRef.current = setInterval(draw, FRAME_INTERVAL);

    return () => {
      if (pingPongRef.current != null) {
        clearInterval(pingPongRef.current);
        pingPongRef.current = null;
      }
    };
  }, [ready]);

  return (
    <div className="absolute inset-0 z-0">
      <div className="h-full w-full scale-[1.15] origin-top overflow-hidden">
        <video
          ref={videoRef}
          src={VIDEO_SRC}
          muted
          playsInline
          preload="auto"
          crossOrigin="anonymous"
          className="h-full w-full object-cover object-top"
          style={{ display: ready ? "none" : "block" }}
        />
        <canvas
          ref={displayCanvasRef}
          className="h-full w-full object-cover object-top"
          style={{ display: ready ? "block" : "none" }}
        />
      </div>
    </div>
  );
}
