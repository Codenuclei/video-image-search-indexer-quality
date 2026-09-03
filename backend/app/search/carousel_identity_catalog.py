"""Identity-wide carousel frame catalog (speaker association + best portraits).

Builds a fresh timestamped appearance catalog from cached video frames using
InsightFace embeddings. Quote windows associate speech with a stable visible
identity; final ranking ignores timestamp proximity and prefers the best
portrait of that person across the whole video.

There is no speaker diarization in this stack — association requires a stable
single face across quote-window samples. Multi-face quote windows recommend a
group/panel frame. Low confidence leaves the slide text-only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

IDENTITY_CATALOG_VERSION = "identity-v1"
_MAX_SCAN_FRAMES = 48
_MAX_APPEARANCES_PER_IDENTITY = 24
_MAX_GROUP_FRAMES = 16
_MAX_EMITTED = 8
_IDENTITY_MATCH_SIM = 0.55
_SPEAKER_VOTE_FRACTION = 0.6
_SPEAKER_MIN_HITS = 2
_MULTI_FACE_PANEL_THRESHOLD = 2


@dataclass
class Appearance:
    frame_ts: float
    identity_id: str
    bbox: tuple[float, float, float, float]  # normalized x,y,w,h
    front_face_score: float
    quality_score: float
    detection_confidence: float
    face_count: int
    embedding: list[float] = field(default_factory=list, repr=False)


@dataclass
class IdentityTrack:
    identity_id: str
    centroid: np.ndarray
    appearances: list[Appearance] = field(default_factory=list)
    person_id: int | None = None
    cluster_id: int | None = None
    label: str = ""

    def update_centroid(self, embedding: list[float]) -> None:
        vec = np.asarray(embedding, dtype=np.float32)
        n = max(1, len(self.appearances))
        self.centroid = (self.centroid * (n - 1) + vec) / float(n)


def normalize_bbox(
    bbox_x: float,
    bbox_y: float,
    bbox_w: float,
    bbox_h: float,
    image_width: int,
    image_height: int,
) -> tuple[float, float, float, float]:
    """Normalize InsightFace pixel boxes to 0–1; leave already-normalized alone."""
    w = float(image_width or 0) or 1.0
    h = float(image_height or 0) or 1.0
    x, y, bw, bh = float(bbox_x), float(bbox_y), float(bbox_w), float(bbox_h)
    # Pixel boxes are typically >> 1.0 in width/height.
    if bw > 1.5 or bh > 1.5 or x > 1.5 or y > 1.5:
        return (
            max(0.0, min(1.0, x / w)),
            max(0.0, min(1.0, y / h)),
            max(0.0, min(1.0, bw / w)),
            max(0.0, min(1.0, bh / h)),
        )
    return (
        max(0.0, min(1.0, x)),
        max(0.0, min(1.0, y)),
        max(0.0, min(1.0, bw)),
        max(0.0, min(1.0, bh)),
    )


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = (float(np.linalg.norm(a)) * float(np.linalg.norm(b))) or 1e-8
    return float(np.dot(a, b) / denom)


def catalog_path(thumbnail_dir: str, drive_file_id: str) -> Path:
    return Path(thumbnail_dir) / "video" / drive_file_id / "identity_catalog.json"


def _front_face_from_norm(
    *,
    x: float,
    y: float,
    w: float,
    h: float,
    confidence: float,
) -> float:
    from app.search.carousel_frame_select import front_face_score

    return front_face_score(
        {
            "bbox_x": x,
            "bbox_y": y,
            "bbox_width": w,
            "bbox_height": h,
            "detection_confidence": confidence,
            "yaw": 0.0,
            "pitch": 0.0,
            "roll": 0.0,
        }
    )


def _score_jpeg(jpeg_bytes: bytes) -> float:
    from app.search.carousel_frame_select import score_frame_quality

    q = score_frame_quality(jpeg_bytes, check_pixelation=False)
    return float(q.get("score") or 0.0)


def _list_scan_timestamps(
    thumbnail_dir: str,
    drive_file_id: str,
    *,
    limit: int = _MAX_SCAN_FRAMES,
) -> list[float]:
    from app.search.carousel_frame_select import index_cached_video_frames

    index = index_cached_video_frames(thumbnail_dir, {drive_file_id}).get(drive_file_id)
    if index is None or not index.timestamps:
        return []
    stamps = list(index.timestamps)
    if len(stamps) <= limit:
        return [round(float(t), 3) for t in stamps]
    # Evenly thin while keeping endpoints.
    step = (len(stamps) - 1) / max(limit - 1, 1)
    picked = [stamps[min(len(stamps) - 1, int(round(i * step)))] for i in range(limit)]
    out: list[float] = []
    seen: set[float] = set()
    for ts in picked:
        key = round(float(ts), 3)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def build_identity_catalog(
    *,
    thumbnail_dir: str,
    drive_file_id: str,
    force: bool = False,
) -> dict[str, Any]:
    """Scan cached frames, cluster faces into identities, persist catalog JSON."""
    fid = (drive_file_id or "").strip()
    path = catalog_path(thumbnail_dir, fid)
    stamps = _list_scan_timestamps(thumbnail_dir, fid)
    fingerprint = catalog_cache_fingerprint(fid, stamps)
    if not force and path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("version") == IDENTITY_CATALOG_VERSION
                and payload.get("fingerprint") == fingerprint
            ):
                return payload
        except Exception:  # noqa: BLE001
            pass

    started = time.monotonic()
    tracks: list[IdentityTrack] = []
    group_frames: list[dict[str, Any]] = []
    frames_scanned = 0

    if stamps:
        try:
            from app.faces.engine import get_face_engine
            import cv2

            from app.search.carousel_frame_select import cached_frame_path

            engine = get_face_engine()
            cv2.setNumThreads(1)
            for ts in stamps:
                frame_path = cached_frame_path(thumbnail_dir, fid, ts)
                if not frame_path.is_file():
                    continue
                image = cv2.imread(str(frame_path))
                if image is None or image.size == 0:
                    continue
                frames_scanned += 1
                img_h, img_w = image.shape[:2]
                try:
                    detections = engine.detect_faces(image)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("identity detect failed %s@%.3f: %s", fid, ts, exc)
                    continue
                quality = _score_jpeg(frame_path.read_bytes())
                face_count = len(detections)
                if face_count >= _MULTI_FACE_PANEL_THRESHOLD:
                    group_frames.append(
                        {
                            "frame_ts": round(float(ts), 3),
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
                        best_i = len(tracks) - 1
                    front = _front_face_from_norm(
                        x=nx,
                        y=ny,
                        w=nw,
                        h=nh,
                        confidence=float(det.confidence),
                    )
                    appearance = Appearance(
                        frame_ts=round(float(ts), 3),
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("identity catalog scan failed drive=%s: %s", fid, str(exc)[:200])

    identities: list[dict[str, Any]] = []
    for track in tracks:
        # Keep strongest appearances only.
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
        "version": IDENTITY_CATALOG_VERSION,
        "drive_file_id": fid,
        "fingerprint": fingerprint,
        "built_at": time.time(),
        "frames_scanned": frames_scanned,
        "identity_count": len(identities),
        "identities": identities,
        "group_frames": group_frames,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".partial.json")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("identity catalog persist failed drive=%s: %s", fid, str(exc)[:160])
    logger.info(
        "identity-catalog drive=%s frames=%d identities=%d groups=%d elapsed_ms=%d",
        fid,
        frames_scanned,
        len(identities),
        len(group_frames),
        payload["elapsed_ms"],
    )
    return payload


def load_or_build_identity_catalog(
    *,
    thumbnail_dir: str,
    drive_file_id: str,
    force: bool = False,
) -> dict[str, Any]:
    return build_identity_catalog(
        thumbnail_dir=thumbnail_dir,
        drive_file_id=drive_file_id,
        force=force,
    )


def associate_quote_identity(
    catalog: dict[str, Any],
    *,
    start_sec: float,
    end_sec: float | None,
) -> dict[str, Any]:
    """Map a quote window to a stable speaker identity or a group panel."""
    start = float(start_sec or 0.0)
    try:
        end = float(end_sec) if end_sec is not None else start
    except (TypeError, ValueError):
        end = start
    if end < start:
        end = start
    # Widen slightly so sparse samples near the cue still vote.
    lo, hi = start - 0.75, end + 0.75

    votes: dict[str, int] = {}
    samples = 0
    multi_face_samples = 0
    best_group: dict[str, Any] | None = None
    identities = {
        str(item.get("id")): item
        for item in (catalog.get("identities") or [])
        if isinstance(item, dict) and item.get("id")
    }

    for identity in identities.values():
        for app in identity.get("appearances") or []:
            if not isinstance(app, dict):
                continue
            try:
                ts = float(app.get("frame_ts"))
            except (TypeError, ValueError):
                continue
            if ts < lo or ts > hi:
                continue
            samples += 1
            face_count = int(app.get("face_count") or 1)
            if face_count >= _MULTI_FACE_PANEL_THRESHOLD:
                multi_face_samples += 1
                if best_group is None or float(app.get("quality_score") or 0) > float(
                    best_group.get("quality_score") or 0
                ):
                    best_group = {
                        "frame_ts": ts,
                        "face_count": face_count,
                        "quality_score": float(app.get("quality_score") or 0),
                        "identity_id": identity.get("id"),
                    }
            else:
                key = str(identity.get("id"))
                votes[key] = votes.get(key, 0) + 1

    # Prefer catalogued group frames inside the window when votes are weak.
    for group in catalog.get("group_frames") or []:
        if not isinstance(group, dict):
            continue
        try:
            ts = float(group.get("frame_ts"))
        except (TypeError, ValueError):
            continue
        if lo <= ts <= hi:
            multi_face_samples += 1
            if best_group is None or float(group.get("quality_score") or 0) > float(
                best_group.get("quality_score") or 0
            ):
                best_group = dict(group)

    if samples == 0 and best_group is None:
        return {
            "mode": "text_only",
            "confidence": 0.0,
            "reason": "no_faces_in_quote_window",
            "identity_id": None,
        }

    if multi_face_samples >= max(1, samples // 2) and best_group is not None:
        return {
            "mode": "group_panel",
            "confidence": min(1.0, multi_face_samples / max(samples, 1)),
            "reason": "multiple_faces_in_quote_window",
            "identity_id": None,
            "panel_frame_ts": round(float(best_group["frame_ts"]), 3),
            "face_count": int(best_group.get("face_count") or 0),
        }

    if not votes:
        return {
            "mode": "text_only",
            "confidence": 0.0,
            "reason": "no_stable_single_face",
            "identity_id": None,
        }

    winner, hits = max(votes.items(), key=lambda kv: kv[1])
    fraction = hits / max(sum(votes.values()), 1)
    if hits < _SPEAKER_MIN_HITS or fraction < _SPEAKER_VOTE_FRACTION:
        return {
            "mode": "text_only",
            "confidence": round(fraction, 3),
            "reason": "unstable_speaker_identity",
            "identity_id": winner,
            "vote_hits": hits,
            "vote_fraction": round(fraction, 3),
        }

    identity = identities.get(winner) or {}
    return {
        "mode": "speaker",
        "confidence": round(fraction, 3),
        "reason": "stable_single_identity",
        "identity_id": winner,
        "identity_label": identity.get("label") or winner,
        "person_id": identity.get("person_id"),
        "vote_hits": hits,
        "vote_fraction": round(fraction, 3),
    }


def _best_appearance(identity: dict[str, Any]) -> dict[str, Any] | None:
    apps = [a for a in (identity.get("appearances") or []) if isinstance(a, dict)]
    if not apps:
        return None
    return max(
        apps,
        key=lambda a: (
            float(a.get("front_face_score") or 0),
            float(a.get("quality_score") or 0),
            float(a.get("detection_confidence") or 0),
        ),
    )


def _portrait_rank_key(app: dict[str, Any]) -> tuple[float, float, float]:
    # Explicitly ignore timestamp proximity — only visual/identity quality.
    return (
        float(app.get("front_face_score") or 0),
        float(app.get("quality_score") or 0),
        float(app.get("detection_confidence") or 0),
    )


def build_slide_identity_candidates(
    catalog: dict[str, Any],
    *,
    drive_file_id: str,
    thumbnail_dir: str,
    association: dict[str, Any],
    max_candidates: int = _MAX_EMITTED,
    prefer_hdr: bool = True,
) -> list[dict[str, Any]]:
    """Build a sectioned candidate directory for one slide."""
    from app.search.carousel_frame_select import (
        cached_frame_path,
        carousel_frame_preview_url,
    )
    from app.video.frame_enhance import ensure_hdr_for_timestamp

    fid = (drive_file_id or "").strip()
    identities = {
        str(item.get("id")): item
        for item in (catalog.get("identities") or [])
        if isinstance(item, dict) and item.get("id")
    }
    items: list[dict[str, Any]] = []
    used_ts: set[float] = set()

    def _add(
        *,
        frame_ts: float,
        category: str,
        identity_id: str | None,
        identity_label: str | None,
        front_face: float,
        quality: float,
        recommended: bool,
        label: str,
    ) -> None:
        key = round(float(frame_ts), 3)
        if key in used_ts:
            return
        source = cached_frame_path(thumbnail_dir, fid, key)
        if not source.is_file():
            return
        hdr_ok = False
        if prefer_hdr:
            hdr_ok = bool(
                ensure_hdr_for_timestamp(thumbnail_dir, fid, key).get("ok")
            )
        preview = carousel_frame_preview_url(fid, key)
        if preview and hdr_ok:
            preview = f"{preview}&variant=hdr"
        used_ts.add(key)
        items.append(
            {
                "frame_ts": key,
                "preview_url": preview,
                "label": label,
                "order": len(items),
                "quality_score": round(float(quality), 4),
                "front_face_score": round(float(front_face), 6),
                "selected": False,
                "recommended": bool(recommended),
                "recommendation_source": "identity" if recommended else None,
                "category": category,
                "identity_id": identity_id,
                "identity_label": identity_label,
                "hdr": bool(hdr_ok),
            }
        )

    mode = str(association.get("mode") or "text_only")
    if mode == "group_panel":
        panel_ts = association.get("panel_frame_ts")
        if panel_ts is not None:
            _add(
                frame_ts=float(panel_ts),
                category="group_panel",
                identity_id=None,
                identity_label=None,
                front_face=0.0,
                quality=float(association.get("quality_score") or 0),
                recommended=True,
                label="Group panel",
            )
    elif mode == "speaker":
        identity_id = str(association.get("identity_id") or "")
        identity = identities.get(identity_id) or {}
        apps = sorted(
            [a for a in (identity.get("appearances") or []) if isinstance(a, dict)],
            key=_portrait_rank_key,
            reverse=True,
        )
        for i, app in enumerate(apps[:3]):
            _add(
                frame_ts=float(app["frame_ts"]),
                category="recommended" if i == 0 else "same_person",
                identity_id=identity_id,
                identity_label=str(
                    association.get("identity_label") or identity.get("label") or identity_id
                ),
                front_face=float(app.get("front_face_score") or 0),
                quality=float(app.get("quality_score") or 0),
                recommended=(i == 0),
                label="AI recommended" if i == 0 else "Same person",
            )

    # Other people — best portrait each.
    speaker_id = str(association.get("identity_id") or "") if mode == "speaker" else ""
    other_ranked: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for identity in identities.values():
        iid = str(identity.get("id") or "")
        if not iid or iid == speaker_id:
            continue
        best = _best_appearance(identity)
        if best is None:
            continue
        other_ranked.append((identity, best))
    other_ranked.sort(key=lambda pair: _portrait_rank_key(pair[1]), reverse=True)
    for identity, best in other_ranked[:4]:
        if len(items) >= max_candidates:
            break
        _add(
            frame_ts=float(best["frame_ts"]),
            category="other_person",
            identity_id=str(identity.get("id")),
            identity_label=str(identity.get("label") or identity.get("id")),
            front_face=float(best.get("front_face_score") or 0),
            quality=float(best.get("quality_score") or 0),
            recommended=False,
            label=str(identity.get("label") or "Other person"),
        )

    # Extra group / panel frames for the directory.
    for group in catalog.get("group_frames") or []:
        if len(items) >= max_candidates:
            break
        if not isinstance(group, dict) or group.get("frame_ts") is None:
            continue
        _add(
            frame_ts=float(group["frame_ts"]),
            category="group_panel",
            identity_id=None,
            identity_label=None,
            front_face=0.0,
            quality=float(group.get("quality_score") or 0),
            recommended=False,
            label="Group panel",
        )

    # Ensure exactly one recommended when we have items and association allowed it.
    if items and not any(item.get("recommended") for item in items):
        if mode in {"speaker", "group_panel"}:
            items[0]["recommended"] = True
            items[0]["recommendation_source"] = "identity"

    for order, item in enumerate(items):
        item["order"] = order
    return items[:max_candidates]


def apply_identity_selection_to_slides(
    slides: list[dict[str, Any]],
    *,
    thumbnail_dir: str,
    drive_file_id: str,
    force_catalog: bool = False,
    prefer_hdr: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach identity-wide candidates; leave slides unselected (text-first)."""
    catalog = load_or_build_identity_catalog(
        thumbnail_dir=thumbnail_dir,
        drive_file_id=drive_file_id,
        force=force_catalog,
    )
    out: list[dict[str, Any]] = []
    modes: dict[str, int] = {}
    for slide in slides:
        item = dict(slide)
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
        # Never auto-apply — studio starts text-only.
        item["preview_url"] = None
        item["frame_ts"] = None
        item["frame_source"] = "identity" if candidates else "heuristic"
        item["instagram_ready"] = False
        item["frame_candidates"] = [float(c["frame_ts"]) for c in candidates]
        item["frame_candidate_items"] = candidates
        item["identity_association"] = {
            k: v
            for k, v in association.items()
            if k != "embedding"
        }
        item["frame_quality"] = {
            "rank_source": "identity",
            "candidates": len(candidates),
            "kept": len(candidates),
            "mode": mode,
            "catalog_identities": int(catalog.get("identity_count") or 0),
        }
        if mode == "text_only" and not candidates:
            item["frame_warning"] = association.get("reason") or "no confident speaker"
        else:
            item.pop("frame_warning", None)
        out.append(item)

    summary = {
        "algorithm": IDENTITY_CATALOG_VERSION,
        "frames_scanned": catalog.get("frames_scanned"),
        "identity_count": catalog.get("identity_count"),
        "modes": modes,
        "slides": len(out),
    }
    return out, summary


def catalog_cache_fingerprint(drive_file_id: str, stamps: list[float]) -> str:
    raw = f"{drive_file_id}:{len(stamps)}:{stamps[0] if stamps else 0}:{stamps[-1] if stamps else 0}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
