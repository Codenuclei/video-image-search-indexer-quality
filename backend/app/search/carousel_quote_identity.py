"""Quote-window-only identity catalog for transcript-first Studio.

Scans hard-capped timestamps derived from each final slide's
``timestamp_sec`` → ``end_timestamp_sec`` (start / midpoint / end), never the
full video. Prefer RunPod ArcFace when configured.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from app.config import Settings, get_settings
from app.search.carousel_identity_catalog import (
    IDENTITY_CATALOG_VERSION,
    Appearance,
    IdentityTrack,
    _MULTI_FACE_PANEL_THRESHOLD,
    _MAX_APPEARANCES_PER_IDENTITY,
    _MAX_GROUP_FRAMES,
    _IDENTITY_MATCH_SIM,
    _cosine,
    _front_face_from_norm,
    _score_jpeg,
    catalog_path,
    normalize_bbox,
)

logger = logging.getLogger(__name__)

QUOTE_WINDOW_CATALOG_VERSION = f"{IDENTITY_CATALOG_VERSION}-quote-v1"
FACE_MODEL_VERSION = "arcface-buffalo-l"


def quote_intervals_from_slides(slides: Iterable[dict[str, Any]]) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        try:
            start = float(slide.get("timestamp_sec") or 0.0)
            end_raw = slide.get("end_timestamp_sec")
            end = float(end_raw) if end_raw is not None else start
        except (TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        intervals.append((round(start, 3), round(end, 3)))
    return intervals


def quote_window_sample_timestamps(
    intervals: list[tuple[float, float]],
    *,
    cap: int = 24,
) -> list[float]:
    """Derive start/mid/end samples per interval; dedupe and hard-cap."""
    raw: list[float] = []
    for start, end in intervals:
        mid = round(start + max(0.0, end - start) * 0.5, 3)
        raw.extend([round(start, 3), mid, round(end, 3)])
    out: list[float] = []
    seen: set[float] = set()
    for ts in raw:
        key = round(float(ts), 3)
        if key < 0 or key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= max(1, int(cap)):
            break
    return out


def quote_catalog_fingerprint(
    drive_file_id: str,
    stamps: list[float],
    intervals: list[tuple[float, float]],
    *,
    face_model_version: str = FACE_MODEL_VERSION,
) -> str:
    interval_key = ",".join(f"{a:.3f}-{b:.3f}" for a, b in intervals[:48])
    stamp_key = ",".join(f"{t:.3f}" for t in stamps)
    raw = f"{drive_file_id}|{face_model_version}|{interval_key}|{stamp_key}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def quote_catalog_path(thumbnail_dir: str, drive_file_id: str) -> Path:
    return Path(thumbnail_dir) / "video" / drive_file_id / "quote_identity_catalog.json"


async def build_quote_window_identity_catalog(
    *,
    thumbnail_dir: str,
    drive_file_id: str,
    slides: list[dict[str, Any]],
    settings: Settings | None = None,
    force: bool = False,
    extract_frame=None,
) -> dict[str, Any]:
    """Build / load a catalog from quote-window frames only.

    ``extract_frame`` is an optional async callable ``(drive_file_id, ts) -> Path|None``
    used to materialize missing JPEGs from the cached video.
    """
    settings = settings or get_settings()
    fid = (drive_file_id or "").strip()
    intervals = quote_intervals_from_slides(slides)
    cap = int(settings.select_images_quote_frame_cap or 24)
    stamps = quote_window_sample_timestamps(intervals, cap=cap)
    fingerprint = quote_catalog_fingerprint(
        fid, stamps, intervals, face_model_version=FACE_MODEL_VERSION
    )
    path = quote_catalog_path(thumbnail_dir, fid)
    if not force and path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("version") == QUOTE_WINDOW_CATALOG_VERSION
                and payload.get("fingerprint") == fingerprint
            ):
                return payload
        except Exception:  # noqa: BLE001
            pass

    # Also accept the legacy whole-video catalog path only when fingerprint matches
    # (normally it won't — quote catalogs use a different key).
    legacy = catalog_path(thumbnail_dir, fid)
    if not force and legacy.is_file() and not stamps:
        try:
            return json.loads(legacy.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass

    started = time.monotonic()
    # Ensure quote frames exist on disk.
    from app.search.carousel_frame_select import cached_frame_path
    import cv2

    frame_images: list[tuple[float, Any]] = []
    for ts in stamps:
        frame_path = cached_frame_path(thumbnail_dir, fid, ts)
        if not frame_path.is_file() and extract_frame is not None:
            try:
                maybe = await extract_frame(fid, ts)
                if maybe is not None:
                    frame_path = Path(maybe)
            except Exception as exc:  # noqa: BLE001
                logger.debug("quote extract failed %s@%.3f: %s", fid, ts, exc)
        if not frame_path.is_file():
            continue
        image = cv2.imread(str(frame_path))
        if image is None or image.size == 0:
            continue
        frame_images.append((float(ts), image, frame_path))

    detections_by_ts: dict[float, list[Any]] = {}
    from app.faces.runpod_face import detect_faces_runpod_frames, runpod_face_configured

    if frame_images and runpod_face_configured(settings):
        try:
            results = await detect_faces_runpod_frames(
                [(ts, img) for ts, img, _ in frame_images],
                settings,
            )
            for row in results:
                detections_by_ts[round(float(row.frame_ts), 3)] = list(row.faces)
        except Exception as exc:  # noqa: BLE001
            logger.warning("RunPod quote-window faces failed for %s: %s", fid, exc)

    if frame_images and len(detections_by_ts) < len(frame_images):
        from app.faces.engine import get_face_engine

        engine = get_face_engine()
        for ts, image, _path in frame_images:
            key = round(float(ts), 3)
            if key in detections_by_ts:
                continue
            try:
                detections_by_ts[key] = list(engine.detect_faces(image))
            except Exception as exc:  # noqa: BLE001
                logger.debug("local quote detect failed %s@%.3f: %s", fid, ts, exc)
                detections_by_ts[key] = []

    tracks: list[IdentityTrack] = []
    group_frames: list[dict[str, Any]] = []
    frames_scanned = 0

    for ts, image, frame_path in frame_images:
        key = round(float(ts), 3)
        detections = detections_by_ts.get(key) or []
        frames_scanned += 1
        img_h, img_w = image.shape[:2]
        try:
            quality = _score_jpeg(Path(frame_path).read_bytes())
        except Exception:  # noqa: BLE001
            quality = 0.0
        face_count = len(detections)
        if face_count >= _MULTI_FACE_PANEL_THRESHOLD:
            group_frames.append(
                {
                    "frame_ts": key,
                    "face_count": face_count,
                    "quality_score": round(quality, 4),
                }
            )
        for det in detections:
            nx, ny, nw, nh = normalize_bbox(
                det.bbox_x,
                det.bbox_y,
                det.bbox_width,
                det.bbox_height,
                img_w,
                img_h,
            )
            emb = np.asarray(det.embedding, dtype=np.float32)
            best_i = -1
            best_sim = -1.0
            for i, track in enumerate(tracks):
                sim = _cosine(emb, track.centroid)
                if sim > best_sim:
                    best_sim = sim
                    best_i = i
            if best_i >= 0 and best_sim >= _IDENTITY_MATCH_SIM:
                track = tracks[best_i]
                identity_id = track.identity_id
            else:
                identity_id = f"id_{len(tracks)}"
                track = IdentityTrack(
                    identity_id=identity_id,
                    centroid=emb.copy(),
                    label=f"Person {len(tracks) + 1}",
                )
                tracks.append(track)
            front = _front_face_from_norm(
                x=nx, y=ny, w=nw, h=nh, confidence=float(det.confidence)
            )
            appearance = Appearance(
                frame_ts=key,
                identity_id=identity_id,
                bbox=(nx, ny, nw, nh),
                front_face_score=front,
                quality_score=round(quality, 4),
                detection_confidence=float(det.confidence),
                face_count=face_count,
                embedding=list(det.embedding),
            )
            track.appearances.append(appearance)
            track.update_centroid(det.embedding)

    identities: list[dict[str, Any]] = []
    for track in tracks:
        centroid_norm = float(np.linalg.norm(track.centroid))
        centroid = (
            track.centroid / centroid_norm if centroid_norm > 1e-8 else track.centroid
        )
        ranked = sorted(
            track.appearances,
            key=lambda a: (a.front_face_score, a.quality_score, a.detection_confidence),
            reverse=True,
        )[:_MAX_APPEARANCES_PER_IDENTITY]
        identities.append(
            {
                "id": track.identity_id,
                "label": track.label or track.identity_id,
                "person_id": track.person_id,
                "cluster_id": track.cluster_id,
                "centroid": [round(float(value), 7) for value in centroid.tolist()],
                "appearance_count": len(track.appearances),
                "appearances": [
                    {
                        "frame_ts": a.frame_ts,
                        "bbox": list(a.bbox),
                        "front_face_score": a.front_face_score,
                        "quality_score": a.quality_score,
                        "detection_confidence": a.detection_confidence,
                        "face_count": a.face_count,
                    }
                    for a in ranked
                ],
            }
        )

    group_frames = sorted(
        group_frames,
        key=lambda g: (float(g.get("quality_score") or 0), int(g.get("face_count") or 0)),
        reverse=True,
    )[:_MAX_GROUP_FRAMES]

    payload: dict[str, Any] = {
        "version": QUOTE_WINDOW_CATALOG_VERSION,
        "drive_file_id": fid,
        "fingerprint": fingerprint,
        "face_model_version": FACE_MODEL_VERSION,
        "quote_intervals": [[a, b] for a, b in intervals],
        "sample_timestamps": stamps,
        "frames_scanned": frames_scanned,
        "identity_count": len(identities),
        "identities": identities,
        "group_frames": group_frames,
        "built_ms": round((time.monotonic() - started) * 1000),
        "scope": "quote_windows",
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".partial.json")
        partial.write_text(json.dumps(payload), encoding="utf-8")
        partial.replace(path)
        # Keep a copy at the legacy path so event-photo helpers that load
        # load_or_build_identity_catalog still see the quote catalog.
        try:
            legacy_partial = legacy.with_suffix(".partial.json")
            legacy_partial.write_text(json.dumps(payload), encoding="utf-8")
            legacy_partial.replace(legacy)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("quote catalog persist failed drive=%s: %s", fid, exc)
    return payload


async def apply_quote_identity_selection_to_slides(
    slides: list[dict[str, Any]],
    *,
    thumbnail_dir: str,
    drive_file_id: str,
    settings: Settings | None = None,
    force_catalog: bool = False,
    prefer_hdr: bool = True,
    extract_frame=None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from app.search.carousel_identity_catalog import (
        associate_quote_identity,
        build_slide_identity_candidates,
        has_explicit_picker_selection,
    )

    catalog = await build_quote_window_identity_catalog(
        thumbnail_dir=thumbnail_dir,
        drive_file_id=drive_file_id,
        slides=slides,
        settings=settings,
        force=force_catalog,
        extract_frame=extract_frame,
    )
    out: list[dict[str, Any]] = []
    modes: dict[str, int] = {}
    for slide in slides:
        item = dict(slide)
        if has_explicit_picker_selection(item):
            modes["manual"] = modes.get("manual", 0) + 1
            out.append(item)
            continue
        start = float(item.get("timestamp_sec") or 0)
        end = item.get("end_timestamp_sec")
        association = associate_quote_identity(catalog, start_sec=start, end_sec=end)
        mode = str(association.get("mode") or "text_only")
        modes[mode] = modes.get(mode, 0) + 1
        candidates = build_slide_identity_candidates(
            catalog,
            drive_file_id=str(item.get("drive_file_id") or drive_file_id),
            thumbnail_dir=thumbnail_dir,
            association=association,
            prefer_hdr=prefer_hdr,
        )
        item["preview_url"] = None
        item["frame_ts"] = None
        item["frame_source"] = "identity" if candidates else "heuristic"
        item["instagram_ready"] = False
        item["frame_candidates"] = [float(c["frame_ts"]) for c in candidates]
        item["frame_candidate_items"] = candidates
        item["identity_association"] = {
            k: v for k, v in association.items() if k != "embedding"
        }
        item["frame_quality"] = {
            "rank_source": "quote_identity",
            "candidates": len(candidates),
            "kept": len(candidates),
            "mode": mode,
            "catalog_identities": int(catalog.get("identity_count") or 0),
            "frames_scanned": int(catalog.get("frames_scanned") or 0),
        }
        if mode == "text_only" and not candidates:
            item["frame_warning"] = association.get("reason") or "no confident speaker"
        else:
            item.pop("frame_warning", None)
        out.append(item)

    summary = {
        "algorithm": QUOTE_WINDOW_CATALOG_VERSION,
        "frames_scanned": catalog.get("frames_scanned"),
        "identity_count": catalog.get("identity_count"),
        "sample_timestamps": catalog.get("sample_timestamps"),
        "modes": modes,
        "slides": len(out),
        "scope": "quote_windows",
    }
    return out, summary
