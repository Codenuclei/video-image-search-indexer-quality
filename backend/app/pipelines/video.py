from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import DriveFile, Face, FaceEmbedding, Media, MediaType, VideoSegment
from app.drive.client import DriveConnectorClient
from app.drive.schemas import ConnectorFolderListing
from app.faces.engine import FaceEngine, get_face_engine
from app.gemini.service import GeminiFileSearchService, get_gemini_service
from app.gemini.video_embeddings import embed_frame_sync
from app.matching.service import assign_face
from app.pipelines.async_cpu import run_cpu_bound
from app.pipelines.common import clear_existing_media, download_to_memory, download_to_temp_file
from app.pipelines.dedup import LocalIdentityTracker, passes_quality_filter
from app.pipelines.image import detect_faces_async
from app.qdrant.client import upsert_frame_sync
from app.video.ffmpeg_utils import extract_frame_at, probe_video, sample_timestamps
from app.video.vtt import VttCue, parse_vtt
from app.video.youtube_cache import video_cache_path
from app.video.youtube_registry import is_youtube_source

logger = logging.getLogger(__name__)


@dataclass
class VideoIndexResult:
    media: Media
    gemini_document_name: str | None


async def _caption_frame(
    gemini: "GeminiFileSearchService",
    frame_path: str,
    timestamp_sec: float,
    settings: Settings,
) -> str:
    """Caption a keyframe: prefer local Qwen3-VL when enabled, fall back to Gemini."""
    if settings.qwen_vlm_enabled:
        try:
            from app.qwen.vlm import describe_image_sync as qwen_describe
            desc = await asyncio.to_thread(
                qwen_describe, frame_path, timestamp_sec=timestamp_sec, settings=settings
            )
            if desc:
                return desc
        except Exception as exc:  # noqa: BLE001
            logger.warning("Qwen VLM caption failed at %.1fs, falling back to Gemini: %s", timestamp_sec, exc)
    if settings.gemini_api_key:
        return await asyncio.to_thread(
            gemini.describe_image, frame_path, timestamp_sec=timestamp_sec
        )
    return ""


def _video_cache_path(settings: Settings, drive_file: DriveFile) -> str:
    return str(video_cache_path(settings, drive_file))


def _frames_dir(settings: Settings, drive_file_id: str) -> Path:
    path = Path(settings.thumbnail_dir) / "video" / drive_file_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _vtt_sibling_id(drive_file: DriveFile, listing: ConnectorFolderListing | None) -> str | None:
    if listing is None:
        return None
    base = drive_file.name.rsplit(".", 1)[0].lower()
    for entry in listing.files:
        if entry.is_folder:
            continue
        name_lower = entry.name.lower()
        if name_lower == f"{base}.vtt":
            return entry.id
        if name_lower.endswith(".vtt") and base in name_lower:
            return entry.id
    return None


async def _load_vtt_cues(
    client: DriveConnectorClient,
    drive_file: DriveFile,
    video_path: str,
    listing: ConnectorFolderListing | None,
) -> list[VttCue]:
    vtt_id = _vtt_sibling_id(drive_file, listing)
    if vtt_id:
        try:
            raw = await download_to_memory(client, vtt_id)
            cues = parse_vtt(raw.decode("utf-8", errors="replace"))
            if cues:
                logger.info("Loaded %d VTT cues from Drive for %s", len(cues), drive_file.name)
                return cues
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load VTT for %s: %s", drive_file.name, exc)

    with tempfile.TemporaryDirectory() as tmp:
        out_vtt = os.path.join(tmp, "subs.vtt")
        cmd = ["ffmpeg", "-y", "-i", video_path, "-map", "0:s:0", out_vtt]
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        if proc.returncode == 0 and os.path.isfile(out_vtt):
            cues = parse_vtt(Path(out_vtt).read_text(encoding="utf-8", errors="replace"))
            if cues:
                logger.info("Extracted %d embedded VTT cues for %s", len(cues), drive_file.name)
                return cues
    return []


def _merge_sample_times(
    duration: float,
    interval_sec: float,
    max_frames: int,
    cue_starts: list[float],
) -> list[float]:
    times = set(sample_timestamps(duration, interval_sec, max_frames))
    for t in cue_starts:
        if 0 <= t < duration:
            times.add(round(t, 3))
    return sorted(times)[: max_frames + len(cue_starts)]


async def _detect_faces_on_frame(
    session: AsyncSession,
    media: Media,
    image_bgr: np.ndarray,
    timestamp_sec: float,
    engine: FaceEngine,
    settings: Settings,
    tracker: LocalIdentityTracker,
) -> None:
    img_h, img_w = image_bgr.shape[:2]
    detections = await detect_faces_async(engine, image_bgr)
    for detection in detections:
        if not passes_quality_filter(detection, img_w, img_h, settings.min_face_area_fraction):
            continue
        local = tracker.match(detection.embedding)
        if local is not None:
            local.update(detection.embedding)
            continue
        face = Face(
            media_id=media.id,
            bbox_x=detection.bbox_x,
            bbox_y=detection.bbox_y,
            bbox_width=detection.bbox_width,
            bbox_height=detection.bbox_height,
            detection_confidence=detection.confidence,
            frame_timestamp=timestamp_sec,
        )
        session.add(face)
        await session.flush()
        from app.pipelines.common import save_face_thumbnail

        face.thumbnail_path = save_face_thumbnail(face.id, detection.thumbnail_jpeg, settings)
        session.add(FaceEmbedding(face_id=face.id, embedding=detection.embedding))
        tracker.register(detection.embedding)
        await assign_face(session, face, detection.embedding)


async def _embed_video_frames_parallel(
    drive_file_id: str,
    frame_paths: dict[float, str],
    settings: Settings,
) -> None:
    """Embed sampled frames concurrently, bounded by gemini_embed_max_concurrent."""
    sem = asyncio.Semaphore(settings.gemini_embed_max_concurrent)

    async def _one(ts: float, frame_path: str) -> None:
        async with sem:
            try:
                vec = await asyncio.to_thread(embed_frame_sync, frame_path)
                await asyncio.to_thread(
                    upsert_frame_sync,
                    drive_file_id=drive_file_id,
                    timestamp=ts,
                    vector=vec,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Gemini frame embed failed at %.1fs for %s: %s",
                    ts,
                    drive_file_id,
                    exc,
                )

    await asyncio.gather(*(_one(ts, path) for ts, path in frame_paths.items()))


async def process_video_file(
    session: AsyncSession,
    drive_file: DriveFile,
    client: DriveConnectorClient,
    settings: Settings | None = None,
    *,
    listing: ConnectorFolderListing | None = None,
    gemini: GeminiFileSearchService | None = None,
    engine: FaceEngine | None = None,
) -> VideoIndexResult:
    """
    Self-hosted video index: cache file, parse VTT, sample frames with ffmpeg,
    detect faces, optional Gemini VLM captions, upload transcript + keyframes to File Search.
    """
    settings = settings or get_settings()
    gemini = gemini or get_gemini_service()
    engine = engine or get_face_engine()

    await clear_existing_media(session, drive_file.id)

    dest = _video_cache_path(settings, drive_file)
    suffix = Path(dest).suffix or ".mp4"

    if is_youtube_source(drive_file):
        if not os.path.isfile(dest) or os.path.getsize(dest) <= 0:
            # Download may have been skipped, or older builds wrote a bad path from
            # title-based "extensions" (e.g. "Ep. #5"). Fetch onto the canonical path.
            from app.video.youtube_local import ensure_youtube_video_local
            from app.video.youtube_registry import youtube_id_from_drive_file

            yt_id = youtube_id_from_drive_file(drive_file)
            if not yt_id:
                raise FileNotFoundError(f"YouTube local cache missing for {drive_file.id}: {dest}")
            drive_file, _downloaded = await ensure_youtube_video_local(session, yt_id)
            dest = _video_cache_path(settings, drive_file)
            if not os.path.isfile(dest) or os.path.getsize(dest) <= 0:
                raise FileNotFoundError(f"YouTube local cache missing for {drive_file.id}: {dest}")
        logger.info("Using shared YouTube library file: %s", dest)
    elif os.path.isfile(dest) and os.path.getsize(dest) > 0:
        logger.info("Reusing cached video: %s", dest)
    else:
        async with download_to_temp_file(client, drive_file.id, settings, suffix=suffix) as tmp:
            shutil.move(tmp, dest)
        logger.info("Cached video: %s", dest)

    probe = await run_cpu_bound(probe_video, dest)
    media = Media(
        drive_file_id=drive_file.id,
        type=MediaType.VIDEO,
        duration_seconds=probe.duration_seconds or None,
    )
    session.add(media)
    await session.flush()

    cues = await _load_vtt_cues(client, drive_file, dest, listing)
    if not cues:
        from app.video.youtube_transcript import fetch_youtube_captions, youtube_id_from_filename

        yt_id = youtube_id_from_filename(drive_file.name)
        if yt_id:
            try:
                cues = await fetch_youtube_captions(yt_id)
                if cues:
                    logger.info(
                        "Loaded %d YouTube caption cues for %s (%s)",
                        len(cues),
                        drive_file.name,
                        yt_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("YouTube caption fetch failed for %s: %s", drive_file.name, exc)

    duration = probe.duration_seconds or 0.0
    sample_times = _merge_sample_times(
        duration,
        settings.video_frame_interval_seconds,
        settings.video_max_sample_frames,
        [c.start_sec for c in cues],
    )

    frames_dir = _frames_dir(settings, drive_file.id)
    tracker = LocalIdentityTracker(settings.media_dedup_similarity_threshold)
    frame_paths: dict[float, str] = {}

    for ts in sample_times:
        frame_path = str(frames_dir / f"{ts:.3f}.jpg")
        ok = await run_cpu_bound(extract_frame_at, dest, ts, frame_path)
        if not ok:
            continue
        frame_paths[ts] = frame_path
        image_bgr = cv2.imread(frame_path)
        if image_bgr is not None:
            await _detect_faces_on_frame(session, media, image_bgr, ts, engine, settings, tracker)

    if settings.gemini_api_key and frame_paths:
        await _embed_video_frames_parallel(drive_file.id, frame_paths, settings)

    cue_by_start = {round(c.start_sec, 3): c for c in cues}
    segments: list[VideoSegment] = []

    for cue in cues:
        frame_path = frame_paths.get(round(cue.start_sec, 3))
        if frame_path is None:
            nearest = min(frame_paths.keys(), key=lambda t: abs(t - cue.start_sec), default=None)
            frame_path = frame_paths.get(nearest) if nearest is not None else None
        segments.append(
            VideoSegment(
                media_id=media.id,
                start_sec=cue.start_sec,
                end_sec=cue.end_sec,
                text=cue.text,
                frame_path=frame_path,
            )
        )
        session.add(segments[-1])

    if not segments and frame_paths:
        for ts, frame_path in sorted(frame_paths.items()):
            session.add(
                VideoSegment(
                    media_id=media.id,
                    start_sec=ts,
                    end_sec=None,
                    text="",
                    frame_path=frame_path,
                )
            )

    await session.flush()

    segments = list(
        (
            await session.execute(select(VideoSegment).where(VideoSegment.media_id == media.id))
        ).scalars().all()
    )

    if settings.video_vlm_enrich and (settings.gemini_api_key or settings.qwen_vlm_enabled):
        for seg in segments:
            if not seg.frame_path or os.path.isfile(seg.frame_path) is False:
                continue
            if seg.text and len(seg.text) > 20:
                continue
            try:
                seg.vlm_description = await _caption_frame(
                    gemini, seg.frame_path, seg.start_sec, settings
                ) or None
            except Exception as exc:  # noqa: BLE001
                logger.warning("VLM describe failed at %.1fs: %s", seg.start_sec, exc)

    transcript_doc: str | None = None
    if settings.gemini_api_key:
        transcript_doc = await _upload_video_index(
            gemini,
            drive_file=drive_file,
            media=media,
            segments=list(segments),
            frame_paths=frame_paths,
            settings=settings,
        )

    logger.info(
        "Indexed video %s: %d segments, %d frames, %d faces",
        drive_file.name,
        len(segments),
        len(frame_paths),
        len(tracker._tracks),
    )
    return VideoIndexResult(media=media, gemini_document_name=transcript_doc)


async def _upload_video_index(
    gemini: GeminiFileSearchService,
    *,
    drive_file: DriveFile,
    media: Media,
    segments: list[VideoSegment],
    frame_paths: dict[float, str],
    settings: Settings,
) -> str | None:
    lines: list[str] = [f"Video: {drive_file.name}", f"Path: {drive_file.path}", ""]
    for seg in segments:
        line = f"[{seg.start_sec:.1f}s] {seg.text}".strip()
        if seg.vlm_description:
            line += f" | Visual: {seg.vlm_description}"
        lines.append(line)

    os.makedirs(settings.temp_dir, exist_ok=True)
    transcript_path = os.path.join(settings.temp_dir, f"{drive_file.id}_transcript.txt")
    Path(transcript_path).write_text("\n".join(lines), encoding="utf-8")

    doc_name = await asyncio.to_thread(
        gemini.upload_file,
        local_path=transcript_path,
        display_name=f"{drive_file.name} (transcript)",
        drive_file_id=drive_file.id,
        drive_path=drive_file.path,
        mime_type="text/plain",
        extra_metadata={
            "content_kind": "video_transcript",
            "timestamp_sec": "0",
        },
    )

    uploaded = 0
    for ts in sorted(frame_paths.keys()):
        if uploaded >= settings.video_max_gemini_frames:
            break
        frame_path = frame_paths[ts]
        seg = next((s for s in segments if abs(s.start_sec - ts) < 0.5), None)
        caption = ((seg.vlm_description or seg.text) if seg else "")[:200]
        await asyncio.to_thread(
            gemini.upload_file,
            local_path=frame_path,
            display_name=f"{drive_file.name} @ {ts:.1f}s",
            drive_file_id=drive_file.id,
            drive_path=drive_file.path,
            mime_type="image/jpeg",
            extra_metadata={
                "content_kind": "video_frame",
                "timestamp_sec": f"{ts:.2f}",
                "caption": caption,
            },
        )
        uploaded += 1

    if os.path.isfile(transcript_path):
        os.remove(transcript_path)
    return doc_name
