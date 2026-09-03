"""Instagram-ready frame selection for carousel slides.

Pipeline per slide (spoken span stays fixed; only the display frame changes):
  1. Sample candidate timestamps across start_sec–end_sec (include heuristic mid-span).
  2. Load JPEG bytes (cache, nearest on disk, optional on-demand extract).
  3. Gemini ranks candidates for Instagram carousel polish.
  4. Gemini readiness flags; walk ranked order until a ready frame (else top / heuristic).
"""

from __future__ import annotations

import asyncio
import bisect
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

DEFAULT_MAX_CANDIDATES = 12
DEFAULT_TIMEOUT_SEC = 32.0
_MAX_JPEG_BYTES = 512 * 1024
_DOWNSCALE_MAX_DIM = 640
_NEAREST_TOLERANCE_SEC = 1.25
# Indexer samples ~1s; widen harvest nearest-match so precomputed JPEGs count
# as hits instead of triggering ffmpeg/Drive extracts.
HARVEST_NEAREST_TOLERANCE_SEC = 2.5
_HARVEST_NEAREST_TOLERANCE_SEC = HARVEST_NEAREST_TOLERANCE_SEC
_HARD_CAP_CANDIDATES = 16
# Studio picker shows a small verified set; never emit more than this.
_MAX_EMITTED_CANDIDATES = 3
# Cold-path budget: never ffmpeg more than this many misses per slide.
_MAX_EXTRACTS_PER_SLIDE = 2
# Skip Gemini when local face+quality already has a clear winner.
_LOCAL_RANK_FACE_THRESHOLD = 0.35
# Ambiguous slides are ranked in batches of 4–6; cap grows with deck size.
_DEFAULT_MAX_GEMINI_RANK_BATCHES = 8
# Persist ranked candidates so re-opening the picker for identical inputs is free.
_CANDIDATE_RESULT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CANDIDATE_RESULT_CACHE_TTL_SEC = 900.0
_CANDIDATE_RESULT_CACHE_MAX = 256


def gemini_rank_batch_limit(
    group_count: int,
    batch_size: int,
    *,
    max_batches: int = _DEFAULT_MAX_GEMINI_RANK_BATCHES,
) -> int:
    """How many Gemini rank requests to run for ``group_count`` ambiguous slides."""
    if group_count <= 0 or batch_size <= 0:
        return 0
    cap = int(max_batches)
    if cap <= 0:
        return 0
    needed = (int(group_count) + int(batch_size) - 1) // int(batch_size)
    return min(needed, cap)


@dataclass(frozen=True)
class FrameCandidate:
    index: int
    timestamp_sec: float
    label: str  # "heuristic" | "sample"
    preview_url: str | None = None
    quality_score: float = 0.0
    front_face: float = 0.0
    perceptual_hash: str | None = None


@dataclass(frozen=True)
class CachedVideoFrameIndex:
    """One directory scan's sorted frame timestamps and paths."""

    timestamps: tuple[float, ...]
    paths: tuple[Path, ...]


def index_cached_video_frames(
    thumbnail_dir: str,
    drive_file_ids: set[str] | list[str] | tuple[str, ...],
) -> dict[str, CachedVideoFrameIndex]:
    """Scan each video's frame directory once for reuse throughout a request."""
    indexed: dict[str, CachedVideoFrameIndex] = {}
    for raw_fid in drive_file_ids:
        fid = str(raw_fid or "").strip()
        if not fid or fid in indexed:
            continue
        frames_dir = Path(thumbnail_dir) / "video" / fid
        entries: list[tuple[float, Path]] = []
        if frames_dir.is_dir():
            for path in frames_dir.glob("*.jpg"):
                try:
                    entries.append((float(path.stem), path))
                except ValueError:
                    continue
        entries.sort(key=lambda item: item[0])
        indexed[fid] = CachedVideoFrameIndex(
            timestamps=tuple(item[0] for item in entries),
            paths=tuple(item[1] for item in entries),
        )
    return indexed


@dataclass
class FramePickResult:
    timestamp_sec: float
    preview_url: str | None
    frame_source: str  # "ai" | "heuristic" | "fallback"
    instagram_ready: bool
    ranked_timestamps: list[float] = field(default_factory=list)
    warning: str | None = None
    quality_stats: dict[str, Any] | None = None
    focal_x: float = 0.5
    focal_y: float = 0.4
    front_face_score: float = 0.0


def heuristic_frame_ts(start_sec: float, end_sec: float | None) -> float:
    """Mid-span default used before Gemini polish (matches outline heuristic)."""
    s = float(start_sec or 0)
    if end_sec is None:
        return round(s, 2)
    try:
        e = float(end_sec)
    except (TypeError, ValueError):
        return round(s, 2)
    if e > s:
        return round(s + (e - s) * 0.5, 2)
    return round(s, 2)


def sample_candidate_timestamps(
    start_sec: float,
    end_sec: float | None,
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    step_sec: float | None = None,
) -> list[float]:
    """Sample timestamps across a spoken span (capped).

    Prefer start / 20% / mid / 80% / end when the window is short; for longer
    windows, also sample about every ``step_sec`` (default 0.4s) then cap.
    Always includes the heuristic mid-span timestamp.
    """
    s = float(start_sec or 0.0)
    e = float(end_sec) if end_sec is not None else s
    if e < s:
        e = s
    dur = e - s
    heuristic = heuristic_frame_ts(s, e if end_sec is not None else None)
    cap = max(1, min(int(max_candidates), _HARD_CAP_CANDIDATES))

    if dur < 0.05:
        return [round(s, 2)]

    fractions = (0.0, 0.15, 0.35, 0.5, 0.65, 0.85, 1.0)
    points = [round(s + dur * f, 2) for f in fractions]

    interval = float(step_sec) if step_sec is not None else 0.4
    if dur >= interval * 2:
        t = s
        while t <= e + 1e-9:
            points.append(round(t, 2))
            t += interval
        points.append(round(e, 2))

    points.append(heuristic)
    # Dedupe while preserving order, then ensure heuristic stays present.
    seen: set[float] = set()
    ordered: list[float] = []
    for ts in points:
        key = round(ts, 2)
        if key in seen:
            continue
        if key < round(s, 2) - 1e-6 or key > round(e, 2) + 1e-6:
            continue
        seen.add(key)
        ordered.append(key)

    if round(heuristic, 2) not in seen:
        ordered.insert(len(ordered) // 2, round(heuristic, 2))

    if len(ordered) <= cap:
        return ordered

    # Keep endpoints + heuristic, fill evenly from the rest.
    keep: list[float] = []
    must = {round(s, 2), round(e, 2), round(heuristic, 2)}
    for ts in ordered:
        if round(ts, 2) in must and ts not in keep:
            keep.append(ts)
    remaining = [ts for ts in ordered if ts not in keep]
    slots = max(0, cap - len(keep))
    if slots and remaining:
        if slots >= len(remaining):
            keep.extend(remaining)
        else:
            step = (len(remaining) - 1) / max(slots - 1, 1)
            for i in range(slots):
                idx = min(len(remaining) - 1, int(round(i * step)))
                ts = remaining[idx]
                if ts not in keep:
                    keep.append(ts)
    keep.sort()
    return keep[:cap]


def sample_candidate_timestamps_multi(
    ranges: list[tuple[float, float | None]],
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    step_sec: float | None = 0.4,
) -> list[float]:
    """Sample across one or more (possibly non-contiguous) time ranges."""
    if not ranges:
        return []
    per = max(3, int(max_candidates) // max(1, len(ranges)))
    points: list[float] = []
    for start, end in ranges:
        points.extend(
            sample_candidate_timestamps(
                start, end, max_candidates=per, step_sec=step_sec
            )
        )
    # Global dedupe + cap
    seen: set[float] = set()
    out: list[float] = []
    for ts in sorted(points):
        key = round(ts, 2)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    cap = max(1, min(int(max_candidates), _HARD_CAP_CANDIDATES))
    if len(out) <= cap:
        return out
    # Evenly thin
    step = (len(out) - 1) / max(cap - 1, 1)
    thinned = [out[min(len(out) - 1, int(round(i * step)))] for i in range(cap)]
    # unique preserve order
    final: list[float] = []
    seen2: set[float] = set()
    for ts in thinned:
        if ts not in seen2:
            seen2.add(ts)
            final.append(ts)
    return final[:cap]


def score_frame_quality(
    jpeg_bytes: bytes | None,
    *,
    check_pixelation: bool = True,
) -> dict[str, Any]:
    """Cheap quality signals: sharpness, exposure, contrast, pixelation. No model calls."""
    stats: dict[str, Any] = {
        "ok": False,
        "sharpness": 0.0,
        "brightness": 0.0,
        "contrast": 0.0,
        "pixelation": 0.0,
        "score": 0.0,
        "reject": None,
        "phash": None,
    }
    if not jpeg_bytes:
        stats["reject"] = "missing"
        return stats
    try:
        import cv2
        import numpy as np

        from app.video.pixelation import PIXELATION_SCORE_THRESHOLD, evaluate_pixelation

        # OpenCV otherwise creates its own worker pool inside every asyncio
        # executor task. Twelve slides can oversubscribe the container badly
        # enough that even the event-loop timeout cannot run on schedule.
        cv2.setNumThreads(1)
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None or bgr.size == 0:
            stats["reject"] = "decode"
            return stats
        h, w = bgr.shape[:2]
        if h < 48 or w < 48:
            stats["reject"] = "tiny"
            return stats
        # Keep a full-res copy for pixelation (NEAREST-downscales internally).
        # Linear/area downscale for sharpness would smear mosaic tiles.
        bgr_full = bgr
        # Downscale for speed
        scale = min(1.0, 320.0 / max(h, w))
        if scale < 1.0:
            bgr = cv2.resize(bgr, (max(1, int(w * scale)), max(1, int(h * scale))))
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        brightness = float(gray.mean())
        contrast = float(gray.std())
        # Very dark / blown / flat / blurry → reject (before pixelation work)
        if brightness < 18:
            stats.update(sharpness=sharp, brightness=brightness, contrast=contrast, reject="black")
            return stats
        if brightness > 245:
            stats.update(sharpness=sharp, brightness=brightness, contrast=contrast, reject="blown")
            return stats
        if contrast < 8:
            stats.update(sharpness=sharp, brightness=brightness, contrast=contrast, reject="flat")
            return stats
        if sharp < 25:
            stats.update(sharpness=sharp, brightness=brightness, contrast=contrast, reject="blurry")
            return stats
        pix_flag = False
        pix_details: dict[str, Any] = {}
        if check_pixelation:
            pix_flag, pix_details = evaluate_pixelation(
                bgr_full, threshold=PIXELATION_SCORE_THRESHOLD
            )
        pix_score = float(pix_details.get("score") or 0.0)
        if pix_flag:
            stats.update(
                sharpness=sharp,
                brightness=brightness,
                contrast=contrast,
                pixelation=pix_score,
                reject="pixelated",
                pixelation_macroblock=float(pix_details.get("macroblock_score") or 0.0),
                pixelation_local_frac=float(pix_details.get("local_mosaic_frac") or 0.0),
            )
            return stats
        # Perceptual-ish hash (8x8 average) for near-dupe detection
        tiny = cv2.resize(gray, (8, 8), interpolation=cv2.INTER_AREA)
        avg = float(tiny.mean())
        bits = "".join("1" if int(v) > avg else "0" for v in tiny.flatten())
        # Prefer mid brightness + high sharpness + decent contrast
        bright_term = 1.0 - abs(brightness - 128.0) / 128.0
        score = sharp * 0.55 + contrast * 2.5 + bright_term * 40.0
        stats.update(
            ok=True,
            sharpness=sharp,
            brightness=brightness,
            contrast=contrast,
            pixelation=pix_score,
            score=score,
            reject=None,
            phash=bits,
            pixelation_macroblock=float(pix_details.get("macroblock_score") or 0.0),
            pixelation_local_frac=float(pix_details.get("local_mosaic_frac") or 0.0),
        )
        return stats
    except Exception as exc:  # noqa: BLE001
        stats["reject"] = f"error:{str(exc)[:40]}"
        return stats


def _hamming(a: str | None, b: str | None) -> int:
    if not a or not b or len(a) != len(b):
        return 64
    return sum(ch1 != ch2 for ch1, ch2 in zip(a, b))


def choose_adjacent_diverse_candidate(
    candidates: list[FrameCandidate],
    ranked_order: list[int],
    proposed_index: int,
    previous_hash: str | None,
    *,
    duplicate_distance: int = 6,
    min_quality_ratio: float = 0.75,
    max_face_drop: float = 0.08,
) -> tuple[int, bool]:
    """Swap a near-duplicate pick for the best acceptable ranked alternate."""
    if (
        not candidates
        or not 0 <= proposed_index < len(candidates)
        or not previous_hash
        or _hamming(candidates[proposed_index].perceptual_hash, previous_hash)
        > duplicate_distance
    ):
        return proposed_index, False
    proposed = candidates[proposed_index]
    quality_floor = max(0.0, proposed.quality_score * min_quality_ratio)
    face_floor = max(0.0, proposed.front_face - max_face_drop)
    for index in ranked_order:
        if not 0 <= index < len(candidates) or index == proposed_index:
            continue
        alternate = candidates[index]
        if not alternate.perceptual_hash:
            continue
        if _hamming(alternate.perceptual_hash, previous_hash) <= duplicate_distance:
            continue
        if alternate.quality_score < quality_floor:
            continue
        if proposed.front_face >= 0.18 and alternate.front_face < face_floor:
            continue
        return index, True
    return proposed_index, False


def filter_frame_candidates_by_quality(
    images: list[bytes | None],
    *,
    timestamps: list[float] | None = None,
    max_keep: int = DEFAULT_MAX_CANDIDATES,
    min_keep: int = 2,
    quality_scores_out: list[dict[str, Any]] | None = None,
    check_pixelation: bool = True,
) -> tuple[list[int], dict[str, Any]]:
    """Filter/rank candidate indices by cheap image quality + phash dedupe.

    Returns ``(kept_indices_in_quality_order, reject_stats)``.
    """
    n = len(images)
    reject_counts: dict[str, int] = {}
    scored: list[tuple[float, int, str | None]] = []  # score, idx, phash
    quality_scores = [
        (
            score_frame_quality(img)
            if check_pixelation
            else score_frame_quality(img, check_pixelation=False)
        )
        for img in images
    ]
    if quality_scores_out is not None:
        quality_scores_out.extend(quality_scores)
    for i, q in enumerate(quality_scores):
        reason = q.get("reject")
        if reason:
            reject_counts[str(reason)] = reject_counts.get(str(reason), 0) + 1
            continue
        scored.append((float(q.get("score") or 0), i, q.get("phash")))

    # If everything rejected, fall back to least-bad — but NEVER re-admit
    # hard quality rejects (pixelation / exposure / blur / flat).
    _HARD_REJECT = {
        "pixelated",
        "black",
        "blown",
        "decode",
        "missing",
        "tiny",
        "flat",
        "blurry",
    }
    if not scored:
        fallback: list[tuple[float, int, str | None]] = []
        for i, q in enumerate(quality_scores):
            reason = str(q.get("reject") or "")
            if reason in _HARD_REJECT or reason.startswith("error:"):
                continue
            fallback.append((float(q.get("score") or 0), i, q.get("phash")))
        fallback.sort(reverse=True)
        if not fallback:
            stats = {
                "rejected": dict(reject_counts),
                "fallback": True,
                "scored": 0,
                "kept": 0,
                "hard_reject_only": True,
            }
            return [], stats
        kept = [i for _, i, _ in fallback[: max(1, min(max_keep, n))]]
        stats = {
            "rejected": dict(reject_counts),
            "fallback": True,
            "scored": 0,
        }
        return kept, stats

    scored.sort(reverse=True)
    kept: list[int] = []
    kept_hashes: list[str] = []
    dupes = 0
    for score, idx, ph in scored:
        if ph and any(_hamming(ph, prev) <= 6 for prev in kept_hashes):
            dupes += 1
            continue
        # Prefer temporal spacing when timestamps provided
        if timestamps and kept:
            ts = float(timestamps[idx])
            if any(abs(ts - float(timestamps[j])) < 0.35 for j in kept):
                dupes += 1
                continue
        kept.append(idx)
        if ph:
            kept_hashes.append(ph)
        if len(kept) >= max_keep:
            break

    # Guarantee a minimum set for Gemini ranking
    if len(kept) < min_keep:
        for _, idx, ph in scored:
            if idx in kept:
                continue
            kept.append(idx)
            if len(kept) >= min_keep:
                break

    stats = {
        "rejected": dict(reject_counts),
        "near_duplicates": dupes,
        "scored": len(scored),
        "fallback": False,
    }
    logger.info(
        "frame quality filter: candidates=%d scored=%d kept=%d rejects=%s dupes=%d",
        n,
        len(scored),
        len(kept),
        reject_counts,
        dupes,
    )
    return kept, stats


def build_frame_candidates(
    drive_file_id: str,
    start_sec: float,
    end_sec: float | None,
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    extra_ranges: list[tuple[float, float | None]] | None = None,
) -> list[FrameCandidate]:
    """Build labeled candidates; heuristic mid-span is marked ``heuristic``."""
    fid = (drive_file_id or "").strip()
    heuristic = heuristic_frame_ts(start_sec, end_sec)
    ranges: list[tuple[float, float | None]] = [(start_sec, end_sec)]
    for r in extra_ranges or []:
        ranges.append(r)
    if len(ranges) > 1:
        stamps = sample_candidate_timestamps_multi(ranges, max_candidates=max_candidates)
    else:
        stamps = sample_candidate_timestamps(start_sec, end_sec, max_candidates=max_candidates)
    out: list[FrameCandidate] = []
    for i, ts in enumerate(stamps):
        label = "heuristic" if abs(ts - heuristic) < 0.011 else "sample"
        url = f"/media/video/{fid}/frame?ts={ts}&cache_only=1" if fid else None
        out.append(FrameCandidate(index=i, timestamp_sec=ts, label=label, preview_url=url))
    # Ensure exactly one heuristic label when possible
    if out and not any(c.label == "heuristic" for c in out):
        mid_i = min(range(len(out)), key=lambda i: abs(out[i].timestamp_sec - heuristic))
        c = out[mid_i]
        out[mid_i] = FrameCandidate(
            index=c.index,
            timestamp_sec=c.timestamp_sec,
            label="heuristic",
            preview_url=c.preview_url,
            quality_score=c.quality_score,
        )
    return out


def pick_ready_from_ranked(
    *,
    order: list[int],
    ready: list[bool] | None,
    n: int,
    heuristic_index: int,
) -> tuple[int, str, bool]:
    """Choose candidate index from ranked order + readiness flags.

    Returns ``(index, frame_source, instagram_ready)``.
    """
    if n <= 0:
        return 0, "heuristic", False

    hi = heuristic_index if 0 <= heuristic_index < n else 0
    valid_order = [i for i in order if isinstance(i, int) and 0 <= i < n]
    # Deduplicate while preserving rank order
    seen: set[int] = set()
    ranked: list[int] = []
    for i in valid_order:
        if i not in seen:
            seen.add(i)
            ranked.append(i)
    # Fill any missing indices after Gemini's order (for readiness walk).
    for i in range(n):
        if i not in seen:
            ranked.append(i)

    flags = ready if ready is not None and len(ready) == n else None
    gemini_ranked = bool(valid_order)

    if flags:
        for i in ranked:
            if flags[i]:
                return i, "ai", True
        if gemini_ranked:
            return ranked[0], "fallback", False
        return hi, "fallback", False

    if gemini_ranked:
        return valid_order[0], "ai", True
    return hi, "heuristic", False


def _downscale_jpeg(jpeg_bytes: bytes, max_dim: int = _DOWNSCALE_MAX_DIM) -> bytes:
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(jpeg_bytes))
        img = img.convert("RGB")
        w, h = img.size
        scale = max(w, h) / float(max_dim)
        if scale > 1.0:
            img = img.resize((max(1, int(w / scale)), max(1, int(h / scale))))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=78)
        return out.getvalue()
    except Exception:  # noqa: BLE001
        return jpeg_bytes


def _parse_rank_response(text: str, n: int) -> tuple[list[int] | None, list[bool] | None]:
    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        return None, None
    try:
        parsed = json.loads(m.group())
    except json.JSONDecodeError:
        return None, None
    if not isinstance(parsed, dict):
        return None, None

    order_raw = parsed.get("order") or parsed.get("ranked_indices") or parsed.get("ranked")
    ready_raw = parsed.get("ready") or parsed.get("instagram_ready_flags")

    order: list[int] | None = None
    if isinstance(order_raw, list):
        cleaned: list[int] = []
        for v in order_raw:
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= i < n and i not in cleaned:
                cleaned.append(i)
        if cleaned:
            order = cleaned

    ready: list[bool] | None = None
    if isinstance(ready_raw, list) and len(ready_raw) == n:
        ready = [bool(v) for v in ready_raw]

    return order, ready


def front_face_score(face: Any) -> float:
    """Score an indexed face for portrait selection without a model call.

    InsightFace metadata is not uniform across older indexes, so accept either
    objects or dictionaries and treat missing pose as neutral. Lower yaw,
    pitch, roll and larger, confident faces are preferred.
    """
    def value(name: str, default: float = 0.0) -> float:
        if isinstance(face, dict):
            raw = face.get(name, default)
        else:
            raw = getattr(face, name, default)
        try:
            return float(raw if raw is not None else default)
        except (TypeError, ValueError):
            return default

    yaw = abs(value("yaw", value("pose_yaw", value("head_pose_yaw", 0.0))))
    pitch = abs(value("pitch", value("pose_pitch", value("head_pose_pitch", 0.0))))
    roll = abs(value("roll", value("pose_roll", value("head_pose_roll", 0.0))))
    confidence = max(0.0, min(1.0, value("detection_confidence", value("confidence", 0.5))))
    x = value("bbox_x", value("x", 0.0))
    y = value("bbox_y", value("y", 0.0))
    w = value("bbox_width", value("width", 0.0))
    h = value("bbox_height", value("height", 0.0))
    # InsightFace stores pixel boxes; older carousel payloads sometimes already
    # use 0–1. Normalize pixel boxes when dimensions are provided or when the
    # box clearly exceeds unit range.
    img_w = value("image_width", value("frame_width", 0.0))
    img_h = value("image_height", value("frame_height", 0.0))
    if w > 1.5 or h > 1.5 or x > 1.5 or y > 1.5:
        if img_w > 1 and img_h > 1:
            x, y, w, h = x / img_w, y / img_h, w / img_w, h / img_h
        else:
            # Best-effort: assume a common HD frame when metadata is missing.
            x, y, w, h = x / 1280.0, y / 720.0, w / 1280.0, h / 720.0
        x = max(0.0, min(1.0, x))
        y = max(0.0, min(1.0, y))
        w = max(0.0, min(1.0, w))
        h = max(0.0, min(1.0, h))
    # Normalized boxes are what the index stores. A border-touching face is
    # unusable for a portrait crop even when its detector confidence is high.
    edge_penalty = 1.0
    if x <= 0.015 or y <= 0.015 or x + w >= 0.985:
        edge_penalty *= 0.35
    if y + h >= 0.985:
        edge_penalty *= 0.5
    # Prefer a horizontally centered subject (chest-up Instagram crop).
    cx = x + w / 2.0
    center_bonus = max(0.0, 1.0 - abs(cx - 0.5) / 0.5)
    area = max(0.0, w * h)
    pose = (
        max(0.0, 1.0 - min(yaw, 90.0) / 90.0) * 0.55
        + max(0.0, 1.0 - min(pitch, 90.0) / 90.0) * 0.25
        + max(0.0, 1.0 - min(roll, 90.0) / 90.0) * 0.20
    )
    # Extreme profiles/back-of-heads should lose decisively to a usable face.
    if yaw >= 60.0 or pitch >= 55.0:
        pose *= 0.08
    elif yaw >= 42.0:
        pose *= 0.3
    # Front-facing + centered: dominate ranking so Gemini cannot pick backs.
    if yaw <= 25.0 and center_bonus >= 0.55 and area >= 0.03:
        pose = max(pose, 0.85)
    return round(
        (pose * 0.55 + confidence * 0.15 + min(area, 0.5) * 0.2 + center_bonus * 0.25)
        * edge_penalty,
        6,
    )


def _face_for_slide(slide: dict[str, Any], timestamp_sec: float) -> dict[str, Any] | None:
    raw = slide.get("faces") or slide.get("face_detections") or slide.get("frame_faces")
    if isinstance(raw, dict):
        raw = raw.get(str(round(timestamp_sec, 2))) or raw.get(round(timestamp_sec, 2))
    if not isinstance(raw, list):
        raw = [raw] if raw else []
    choices: list[dict[str, Any]] = []
    for face in raw:
        if not isinstance(face, dict):
            continue
        face_ts = face.get("timestamp_sec", face.get("frame_timestamp", face.get("ts")))
        if face_ts is not None:
            try:
                if abs(float(face_ts) - timestamp_sec) > 2.0:
                    continue
            except (TypeError, ValueError):
                pass
        choices.append(face)
    return max(choices, key=front_face_score, default=None)


def focal_point_for_slide(slide: dict[str, Any], timestamp_sec: float) -> tuple[float, float, float]:
    face = _face_for_slide(slide, timestamp_sec)
    if not face:
        return 0.5, 0.4, 0.0
    try:
        x = float(face.get("bbox_x") or face.get("x") or 0.0)
        y = float(face.get("bbox_y") or face.get("y") or 0.0)
        w = float(face.get("bbox_width") or face.get("width") or 0.0)
        h = float(face.get("bbox_height") or face.get("height") or 0.0)
    except (TypeError, ValueError):
        return 0.5, 0.4, front_face_score(face)
    return (
        max(0.0, min(1.0, x + w / 2.0)),
        max(0.0, min(1.0, y + h * 0.42)),
        front_face_score(face),
    )


def _front_face_for_slide(slide: dict[str, Any], timestamp_sec: float) -> float:
    """Return the best indexed/heuristic face score for a candidate timestamp.

    Penalize audience-style multi-face frames and tiny/edge faces that read as
    torso crops so Instagram picks prefer a single centered speaker.
    """
    raw = slide.get("faces") or slide.get("face_detections") or slide.get("frame_faces")
    if isinstance(raw, dict):
        # Indexed payloads commonly use either timestamp keys or a faces list.
        raw = raw.get(str(round(timestamp_sec, 2))) or raw.get(round(timestamp_sec, 2))
    if not isinstance(raw, list):
        raw = [raw] if raw else []
    scored: list[float] = []
    near_faces: list[dict[str, Any]] = []
    for face in raw:
        if not isinstance(face, dict):
            scored.append(front_face_score(face))
            continue
        face_ts = face.get(
            "timestamp_sec", face.get("frame_timestamp", face.get("ts"))
        )
        if face_ts is not None:
            try:
                if abs(float(face_ts) - timestamp_sec) > 2.0:
                    continue
            except (TypeError, ValueError):
                pass
        near_faces.append(face)
        # InsightFace landmarks can expose yaw as pose_yaw or head_pose_yaw.
        scored.append(front_face_score(face))
    if not scored:
        return 0.0
    best = max(scored)
    # Audience / group shot: many faces near this timestamp → hard demote.
    if len(near_faces) >= 3:
        best *= 0.12
    elif len(near_faces) == 2:
        best *= 0.55
    # Tiny face ≈ torso / wide establishing shot — demote for chest-up IG crop.
    for face in near_faces:
        try:
            area = float(face.get("bbox_width") or face.get("width") or 0) * float(
                face.get("bbox_height") or face.get("height") or 0
            )
        except (TypeError, ValueError):
            area = 0.0
        if area > 0 and area < 0.02:
            best *= 0.35
            break
    return round(best, 6)


def _parse_grouped_rank_response(
    text: str, groups: list[list[FrameCandidate]]
) -> dict[int, tuple[list[int] | None, list[bool] | None]]:
    """Parse one Gemini response containing rankings for multiple slides."""
    try:
        match = re.search(r"\{[\s\S]*\}", text or "")
        data = json.loads(match.group()) if match else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    raw_groups = data.get("slides") if isinstance(data, dict) else None
    if raw_groups is None and isinstance(data, dict):
        raw_groups = data.get("groups") or data.get("rankings")
    if isinstance(raw_groups, dict):
        raw_groups = [
            {"slide": key, **(value if isinstance(value, dict) else {})}
            for key, value in raw_groups.items()
        ]
    if not isinstance(raw_groups, list):
        return {}
    result: dict[int, tuple[list[int] | None, list[bool] | None]] = {}
    for pos, item in enumerate(raw_groups):
        if not isinstance(item, dict):
            continue
        try:
            group_idx = int(
                item.get("slide", item.get("slide_index", item.get("group", pos)))
            )
        except (TypeError, ValueError):
            group_idx = pos
        if not 0 <= group_idx < len(groups):
            continue
        order, ready = _parse_rank_response(json.dumps(item), len(groups[group_idx]))
        if order:
            result[group_idx] = (order, ready)
    return result


def _group_rank_prompt(
    groups: list[tuple[int, str, list[FrameCandidate]]],
    *,
    style_copy_refs: list[str] | None = None,
) -> str:
    parts = [
        "Rank candidates for multiple Instagram carousel slides. "
        "Return ONLY JSON with a slides array. Each item must contain "
        "slide (group index), order (best to worst), and ready flags.",
    ]
    clean_copy = [c.strip() for c in (style_copy_refs or []) if (c or "").strip()][:6]
    if clean_copy:
        parts.append(
            "\nAttached COPY references (tone/angle inspiration for frame choice):\n"
            + "\n".join(f"- {c[:300]}" for c in clean_copy)
        )
        parts.append(
            "\nPrefer frames whose mood/composition fits those references when possible."
        )
    for group_idx, hook, candidates in groups:
        parts.append(
            f'\nSlide {group_idx}, spoken text: "{(hook or "").strip()[:240]}"\n'
            + ", ".join(
                f"{c.index}: {c.label} @{c.timestamp_sec:.2f}s"
                for c in candidates
            )
        )
    parts.append(
        '\nSchema: {"slides":[{"slide":0,"order":[0,1],'
        '"ready":[true,false]}]}'
    )
    return "".join(parts)


def _grouped_rank_blocks(
    groups: list[tuple[int, str, list[FrameCandidate], list[bytes | None]]],
    *,
    style_copy_refs: list[str] | None = None,
    style_image_bytes: list[bytes] | None = None,
) -> list[tuple[str, bytes | None]]:
    prompt_groups = [
        (local_i, hook, candidates)
        for local_i, (_, hook, candidates, _) in enumerate(groups)
    ]
    blocks: list[tuple[str, bytes | None]] = [
        (_group_rank_prompt(prompt_groups, style_copy_refs=style_copy_refs), None)
    ]
    for i, raw in enumerate((style_image_bytes or [])[:4]):
        blocks.append((f"STYLE REFERENCE IMAGE {i + 1}:", _downscale_jpeg(raw)))
    for local_i, (_, _, candidates, images) in enumerate(groups):
        blocks.append((f"SLIDE {local_i} CANDIDATES", None))
        for candidate, image in zip(candidates, images):
            label = (
                f"Slide {local_i} candidate {candidate.index} "
                f"({candidate.label} @{candidate.timestamp_sec:.2f}s):"
            )
            blocks.append((label, _downscale_jpeg(image) if image else None))
    return blocks


def _openai_vision_content(blocks: list[tuple[str, bytes | None]]) -> list[dict[str, Any]]:
    import base64

    content: list[dict[str, Any]] = []
    for text, jpeg in blocks:
        if text:
            content.append({"type": "text", "text": text})
        if jpeg:
            b64 = base64.b64encode(jpeg).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                }
            )
    return content


def _map_local_rank_result(
    groups: list[tuple[int, str, list[FrameCandidate], list[bytes | None]]],
    raw_text: str,
) -> dict[int, tuple[list[int] | None, list[bool] | None]]:
    local_result = _parse_grouped_rank_response(
        raw_text, [candidates for _, _, candidates, _ in groups]
    )
    return {
        groups[local_i][0]: value
        for local_i, value in local_result.items()
        if 0 <= local_i < len(groups)
    }


def rank_grouped_candidates_with_openrouter_sync(
    *,
    groups: list[tuple[int, str, list[FrameCandidate], list[bytes | None]]],
    api_key: str,
    model: str,
    base_url: str = "",
    style_copy_refs: list[str] | None = None,
    style_image_bytes: list[bytes] | None = None,
) -> dict[int, tuple[list[int] | None, list[bool] | None]]:
    if not api_key or not model or not groups:
        return {}
    from app.llm.openrouter import complete_vision_json_sync

    text = complete_vision_json_sync(
        _openai_vision_content(
            _grouped_rank_blocks(
                groups,
                style_copy_refs=style_copy_refs,
                style_image_bytes=style_image_bytes,
            )
        ),
        model=model,
        api_key=api_key,
        base_url=base_url or "https://openrouter.ai/api/v1",
        system="Return ONLY valid JSON that ranks the attached slide frames.",
        max_tokens=2048,
        timeout=28.0,
    )
    return _map_local_rank_result(groups, text)


def rank_grouped_candidates_with_claude_sync(
    *,
    groups: list[tuple[int, str, list[FrameCandidate], list[bytes | None]]],
    api_key: str,
    model: str,
    style_copy_refs: list[str] | None = None,
    style_image_bytes: list[bytes] | None = None,
) -> dict[int, tuple[list[int] | None, list[bool] | None]]:
    if not api_key or not model or not groups:
        return {}
    import base64

    from anthropic import Anthropic

    content: list[dict[str, Any]] = []
    for text, jpeg in _grouped_rank_blocks(
        groups,
        style_copy_refs=style_copy_refs,
        style_image_bytes=style_image_bytes,
    ):
        if text:
            content.append({"type": "text", "text": text})
        if jpeg:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(jpeg).decode("ascii"),
                    },
                }
            )
    response = Anthropic(api_key=api_key).messages.create(
        model=model,
        max_tokens=2048,
        system="Return ONLY valid JSON that ranks the attached slide frames.",
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(
        block.text
        for block in response.content
        if getattr(block, "type", "") == "text"
    )
    return _map_local_rank_result(groups, text)


def rank_grouped_candidates(
    *,
    groups: list[tuple[int, str, list[FrameCandidate], list[bytes | None]]],
    llm_pack: dict[str, Any] | None = None,
    api_key: str = "",
    model: str = "",
    style_copy_refs: list[str] | None = None,
    style_image_bytes: list[bytes] | None = None,
) -> dict[int, tuple[list[int] | None, list[bool] | None]]:
    """Rank frames with the studio picker (OpenRouter / Claude / Gemini)."""
    from app.llm.carousel_llm import vision_hops

    hops: list[tuple[str, str, str]] = []
    or_base = ""
    if llm_pack:
        hops = vision_hops(llm_pack)
        or_base = str(llm_pack.get("openrouter_base_url") or "")
    if not hops and api_key and model:
        hops = [("gemini", model, api_key)]
    for hop, hop_model, hop_key in hops:
        try:
            if hop == "openrouter":
                result = rank_grouped_candidates_with_openrouter_sync(
                    groups=groups,
                    api_key=hop_key,
                    model=hop_model,
                    base_url=or_base,
                    style_copy_refs=style_copy_refs,
                    style_image_bytes=style_image_bytes,
                )
            elif hop == "claude":
                result = rank_grouped_candidates_with_claude_sync(
                    groups=groups,
                    api_key=hop_key,
                    model=hop_model,
                    style_copy_refs=style_copy_refs,
                    style_image_bytes=style_image_bytes,
                )
            else:
                result = rank_grouped_candidates_with_gemini_sync(
                    groups=groups,
                    api_key=hop_key,
                    model=hop_model,
                    style_copy_refs=style_copy_refs,
                    style_image_bytes=style_image_bytes,
                )
            if result:
                return result
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "carousel frame rank hop %s failed (%s) — trying next",
                hop,
                str(exc)[:160],
            )
    return {}


def rank_grouped_candidates_with_gemini_sync(
    *,
    groups: list[tuple[int, str, list[FrameCandidate], list[bytes | None]]],
    api_key: str,
    model: str,
    style_copy_refs: list[str] | None = None,
    style_image_bytes: list[bytes] | None = None,
) -> dict[int, tuple[list[int] | None, list[bool] | None]]:
    """Rank several slides in one multimodal Gemini request."""
    if not api_key or not groups:
        return {}
    from google import genai
    from google.genai import types

    # Gemini receives batch-local slide ids; callers can use stable global ids.
    prompt_groups = [
        (local_i, hook, candidates)
        for local_i, (_, hook, candidates, _) in enumerate(groups)
    ]
    parts: list = [
        types.Part(text=_group_rank_prompt(prompt_groups, style_copy_refs=style_copy_refs))
    ]
    for i, raw in enumerate((style_image_bytes or [])[:4]):
        parts.append(types.Part(text=f"STYLE REFERENCE IMAGE {i + 1}:"))
        parts.append(
            types.Part.from_bytes(data=_downscale_jpeg(raw), mime_type="image/jpeg")
        )
    for local_i, (_, _, candidates, images) in enumerate(groups):
        parts.append(types.Part(text=f"SLIDE {local_i} CANDIDATES"))
        for candidate, image in zip(candidates, images):
            parts.append(
                types.Part(
                    text=(
                        f"Slide {local_i} candidate {candidate.index} "
                        f"({candidate.label} @{candidate.timestamp_sec:.2f}s):"
                    )
                )
            )
            parts.append(
                types.Part.from_bytes(
                    data=_downscale_jpeg(image), mime_type="image/jpeg"
                )
                if image
                else types.Part(text="[image unavailable]")
            )
    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(
                temperature=0.1, response_mime_type="application/json"
            ),
        )
        local_result = _parse_grouped_rank_response(
            response.text or "", [candidates for _, _, candidates, _ in groups]
        )
        return {
            groups[local_i][0]: value
            for local_i, value in local_result.items()
            if 0 <= local_i < len(groups)
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("grouped carousel frame rank failed: %s", str(exc)[:180])
        return {}


def _rank_prompt(hook_line: str, candidates: list[FrameCandidate]) -> str:
    labels = ", ".join(
        f"{c.index}: {c.label} @{c.timestamp_sec:.2f}s" for c in candidates
    )
    hook = (hook_line or "").strip()[:280] or "(no spoken text)"
    return (
        "You are polishing frames for an Instagram carousel slide.\n"
        f'Spoken / hook text on this slide: "{hook}"\n'
        f"Candidate frames (0-based indices): {labels}\n"
        "The candidate labeled heuristic is the current default (mid spoken span).\n\n"
        "Rank ALL candidates best→worst for Instagram display:\n"
        "- SINGLE subject only — one speaker, chest-up / talking-head portrait\n"
        "- face clearly visible, front-facing toward camera (not profile, not back of head)\n"
        "- subject centered horizontally; face in the upper-middle of the frame\n"
        "- REJECT back-facing, extreme profile, audience/crowd, empty stages, and torso-only crops\n"
        "- if multiple candidates have a usable front face, pick the most centered one\n"
        "- if none have a clear front face, pick the least-bad usable frame (still avoid backs)\n"
        "- REJECT audience groups, crowded rooms, backs of heads, extreme profiles\n"
        "- REJECT torso-only / neck-down crops with no usable face\n"
        "- good composition, not awkward crop or cut-off heads\n"
        "- not transitional blur, mid-blink, or UI chrome junk\n"
        "- readable when short text overlays the bottom third\n\n"
        "Also flag each candidate as Instagram-ready (true/false) by the same bar.\n\n"
        "Return ONLY JSON:\n"
        '{"order":[best_index,...],"ready":[true/false per candidate in index order 0..n-1]}\n'
        f"order must list each index 0..{len(candidates) - 1} exactly once."
    )


def rank_candidates_with_gemini_sync(
    *,
    hook_line: str,
    candidates: list[FrameCandidate],
    images: list[bytes | None],
    api_key: str,
    model: str,
) -> tuple[list[int] | None, list[bool] | None]:
    """Multimodal Gemini rank + readiness. On failure returns ``(None, None)``."""
    n = len(candidates)
    if not api_key or n == 0:
        return None, None

    usable = [(i, img) for i, img in enumerate(images) if img]
    if len(usable) < 1:
        return None, None

    from google import genai
    from google.genai import types

    parts: list = [types.Part(text=_rank_prompt(hook_line, candidates))]
    for c, img in zip(candidates, images):
        parts.append(types.Part(text=f"Candidate {c.index} ({c.label} @ {c.timestamp_sec:.2f}s):"))
        if img:
            parts.append(types.Part.from_bytes(data=_downscale_jpeg(img), mime_type="image/jpeg"))
        else:
            parts.append(types.Part(text="[image unavailable]"))

    client = genai.Client(api_key=api_key)
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=parts)],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                ),
            )
            order, ready = _parse_rank_response(resp.text or "", n)
            if order:
                return order, ready
            logger.warning("carousel frame select: unparseable Gemini response")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            msg = str(exc)
            if any(c in msg for c in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "500")):
                time.sleep(1.5 * (attempt + 1))
                continue
            logger.warning("carousel frame select Gemini failed: %s", msg[:180])
            break
    if last_exc:
        logger.warning("carousel frame select gave up: %s", str(last_exc)[:160])
    return None, None


def cached_frame_path(thumbnail_dir: str, drive_file_id: str, ts: float) -> Path:
    return Path(thumbnail_dir) / "video" / drive_file_id / f"{ts:.3f}.jpg"


def carousel_frame_preview_url(drive_file_id: str, ts: float) -> str | None:
    """Cache-only 4:5 preview URL for carousel/studio clients."""
    fid = (drive_file_id or "").strip()
    if not fid:
        return None
    return f"/media/video/{fid}/frame?ts={float(ts):.3f}&cache_only=1&ar=4x5"


def frame_candidate_item(
    *,
    drive_file_id: str,
    candidate: FrameCandidate,
    order: int,
    selected: bool = False,
    recommended: bool = False,
    recommendation_source: str | None = None,
) -> dict[str, Any]:
    """JSON-safe candidate a human can pick without re-harvesting.

    ``preview_url`` is only emitted when the timestamp is known; callers must
    canonicalize to an on-disk JPEG before building items so ``cache_only``
    GETs do not 404.
    """
    preview = carousel_frame_preview_url(drive_file_id, candidate.timestamp_sec)
    item: dict[str, Any] = {
        "frame_ts": round(float(candidate.timestamp_sec), 3),
        "preview_url": preview,
        "label": candidate.label,
        "order": int(order),
        "quality_score": round(float(candidate.quality_score or 0.0), 4),
        "front_face_score": round(float(candidate.front_face or 0.0), 6),
        "selected": bool(selected),
        "recommended": bool(recommended),
    }
    if recommendation_source:
        item["recommendation_source"] = str(recommendation_source)
    elif recommended:
        item["recommendation_source"] = "local"
    return item


def resolve_cached_frame(
    thumbnail_dir: str,
    drive_file_id: str,
    ts: float,
    *,
    nearest_tolerance_sec: float = _NEAREST_TOLERANCE_SEC,
    cached_frames: CachedVideoFrameIndex | None = None,
) -> tuple[float, bytes] | None:
    """Return ``(canonical_ts, jpeg_bytes)`` for the on-disk frame that would load.

    Always retargets to the actual cached stem so ``cache_only`` preview URLs
    resolve. Returns ``None`` when no usable JPEG exists within tolerance.
    """
    nearest = nearest_cached_frame(
        thumbnail_dir,
        drive_file_id,
        ts,
        nearest_tolerance_sec=nearest_tolerance_sec,
        cached_frames=cached_frames,
    )
    if nearest is None:
        return None
    canon_ts, path = nearest
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data or len(data) > _MAX_JPEG_BYTES:
        return None
    return round(float(canon_ts), 3), data


def _frame_pick_cache_key(
    slide: dict[str, Any],
    *,
    model: str,
    prefer_local: bool,
    max_candidates: int,
) -> str:
    payload = {
        "drive_file_id": str(slide.get("drive_file_id") or "").strip(),
        "timestamp_sec": round(float(slide.get("timestamp_sec") or 0), 3),
        "end_timestamp_sec": (
            round(float(slide["end_timestamp_sec"]), 3)
            if slide.get("end_timestamp_sec") is not None
            else None
        ),
        "transcript": str(
            slide.get("transcript_text")
            or slide.get("hook_line")
            or slide.get("snippet")
            or ""
        ).strip()[:240],
        "model": str(model or ""),
        "prefer_local": bool(prefer_local),
        "max_candidates": int(max_candidates),
        "frame_locked": bool(slide.get("frame_locked"))
        or str(slide.get("frame_source") or "").strip().lower() == "manual",
        "frame_ts": (
            round(float(slide["frame_ts"]), 3) if slide.get("frame_ts") is not None else None
        ),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _candidate_cache_get(key: str) -> dict[str, Any] | None:
    hit = _CANDIDATE_RESULT_CACHE.get(key)
    if not hit:
        return None
    expires_at, payload = hit
    if expires_at < time.monotonic():
        _CANDIDATE_RESULT_CACHE.pop(key, None)
        return None
    return dict(payload)


def _candidate_cache_put(key: str, payload: dict[str, Any]) -> None:
    if len(_CANDIDATE_RESULT_CACHE) >= _CANDIDATE_RESULT_CACHE_MAX:
        # Drop the oldest ~25% by expiry timestamp.
        doomed = sorted(_CANDIDATE_RESULT_CACHE.items(), key=lambda kv: kv[1][0])[
            : max(1, _CANDIDATE_RESULT_CACHE_MAX // 4)
        ]
        for doomed_key, _ in doomed:
            _CANDIDATE_RESULT_CACHE.pop(doomed_key, None)
    _CANDIDATE_RESULT_CACHE[key] = (
        time.monotonic() + _CANDIDATE_RESULT_CACHE_TTL_SEC,
        dict(payload),
    )


def _frame_fields_for_cache(slide: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "frame_ts",
        "preview_url",
        "frame_source",
        "instagram_ready",
        "frame_warning",
        "frame_candidates",
        "frame_candidate_items",
        "frame_quality",
        "frame_diversity",
        "focal_x",
        "focal_y",
        "front_face_score",
    )
    return {k: slide.get(k) for k in keys}


def nearest_cached_frame(
    thumbnail_dir: str,
    drive_file_id: str,
    ts: float,
    *,
    nearest_tolerance_sec: float = HARVEST_NEAREST_TOLERANCE_SEC,
    exclude_ts: set[float] | None = None,
    cached_frames: CachedVideoFrameIndex | None = None,
) -> tuple[float, Path] | None:
    """Return (timestamp, path) for the closest on-disk JPEG within tolerance.

    Used when exact-ts extract is impossible (e.g. YouTube local file missing)
    but the indexer already wrote nearby frames. Callers must retarget
    ``frame_ts`` / ``preview_url`` to the returned timestamp — ``cache_only``
    GETs refuse nearest-neighbour substitution.
    """
    fid = (drive_file_id or "").strip()
    if not fid:
        return None
    excluded = {round(float(x), 3) for x in (exclude_ts or set())}
    if cached_frames is not None:
        choices = (
            (abs(cand - float(ts)), cand, path)
            for cand, path in zip(cached_frames.timestamps, cached_frames.paths)
            if round(cand, 3) not in excluded
        )
        nearest = min(choices, default=None, key=lambda item: item[0])
        if nearest is None or nearest[0] > float(nearest_tolerance_sec):
            return None
        return round(nearest[1], 3), nearest[2]

    frames_dir = Path(thumbnail_dir) / "video" / fid
    if not frames_dir.is_dir():
        return None
    best: Path | None = None
    best_ts = 0.0
    best_dist = float("inf")
    for p in frames_dir.glob("*.jpg"):
        try:
            cand = float(p.stem)
        except ValueError:
            continue
        if round(cand, 3) in excluded:
            continue
        if not p.is_file() or p.stat().st_size <= 0:
            continue
        dist = abs(cand - float(ts))
        if dist < best_dist:
            best_dist = dist
            best = p
            best_ts = cand
    if best is None or best_dist > float(nearest_tolerance_sec):
        return None
    return round(best_ts, 3), best


def list_cached_timestamps_in_span(
    thumbnail_dir: str,
    drive_file_id: str,
    start_sec: float,
    end_sec: float | None,
    *,
    pad_sec: float = 0.75,
    limit: int = 24,
    cached_frames: CachedVideoFrameIndex | None = None,
) -> list[float]:
    """Return on-disk frame timestamps inside a spoken span (fast path)."""
    fid = (drive_file_id or "").strip()
    if not fid:
        return []
    s = float(start_sec or 0.0)
    try:
        e = float(end_sec) if end_sec is not None else s
    except (TypeError, ValueError):
        e = s
    if e < s:
        e = s
    lo, hi = s - pad_sec, e + pad_sec
    if cached_frames is not None:
        left = bisect.bisect_left(cached_frames.timestamps, lo - 1e-6)
        right = bisect.bisect_right(cached_frames.timestamps, hi + 1e-6)
        found = [round(ts, 3) for ts in cached_frames.timestamps[left:right]]
    else:
        frames_dir = Path(thumbnail_dir) / "video" / fid
        if not frames_dir.is_dir():
            return []
        found = []
        for p in frames_dir.glob("*.jpg"):
            try:
                ts = float(p.stem)
            except ValueError:
                continue
            if lo - 1e-6 <= ts <= hi + 1e-6 and p.stat().st_size > 0:
                found.append(round(ts, 3))
        found.sort()
    if len(found) <= limit:
        return found
    # Evenly thin while keeping endpoints.
    step = (len(found) - 1) / max(limit - 1, 1)
    thinned = [found[min(len(found) - 1, int(round(i * step)))] for i in range(limit)]
    out: list[float] = []
    seen: set[float] = set()
    for ts in thinned:
        if ts not in seen:
            seen.add(ts)
            out.append(ts)
    return out[:limit]


def load_cached_frame_bytes(
    thumbnail_dir: str,
    drive_file_id: str,
    ts: float,
    *,
    nearest_tolerance_sec: float = _NEAREST_TOLERANCE_SEC,
    cached_frames: CachedVideoFrameIndex | None = None,
) -> bytes | None:
    """Load exact or nearest cached JPEG under the video frames dir."""
    resolved = resolve_cached_frame(
        thumbnail_dir,
        drive_file_id,
        ts,
        nearest_tolerance_sec=nearest_tolerance_sec,
        cached_frames=cached_frames,
    )
    return None if resolved is None else resolved[1]


def build_cache_first_candidates(
    drive_file_id: str,
    start_sec: float,
    end_sec: float | None,
    *,
    thumbnail_dir: str,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    cached_frames: CachedVideoFrameIndex | None = None,
) -> list[FrameCandidate]:
    """Prefer precomputed on-disk frames in-span; fill gaps with span samples."""
    fid = (drive_file_id or "").strip()
    heuristic = heuristic_frame_ts(start_sec, end_sec)
    cap = max(1, min(int(max_candidates), _HARD_CAP_CANDIDATES))
    cached = list_cached_timestamps_in_span(
        thumbnail_dir,
        fid,
        start_sec,
        end_sec,
        limit=cap,
        cached_frames=cached_frames,
    )
    samples = sample_candidate_timestamps(start_sec, end_sec, max_candidates=cap)
    stamps: list[float] = []
    seen: set[float] = set()

    def _add(ts: float) -> None:
        key = round(float(ts), 2)
        if key in seen:
            return
        seen.add(key)
        stamps.append(round(float(ts), 2))

    _add(heuristic)
    for ts in cached:
        _add(ts)
        if len(stamps) >= cap:
            break
    if len(stamps) < cap:
        for ts in samples:
            _add(ts)
            if len(stamps) >= cap:
                break
    stamps = stamps[:cap]
    out: list[FrameCandidate] = []
    for i, ts in enumerate(stamps):
        label = "heuristic" if abs(ts - heuristic) < 0.011 else "sample"
        url = carousel_frame_preview_url(fid, ts)
        out.append(FrameCandidate(index=i, timestamp_sec=ts, label=label, preview_url=url))
    if out and not any(c.label == "heuristic" for c in out):
        mid_i = min(range(len(out)), key=lambda i: abs(out[i].timestamp_sec - heuristic))
        c = out[mid_i]
        out[mid_i] = FrameCandidate(
            index=c.index,
            timestamp_sec=c.timestamp_sec,
            label="heuristic",
            preview_url=c.preview_url,
            quality_score=c.quality_score,
        )
    return out


async def select_frame_for_span(
    *,
    drive_file_id: str,
    start_sec: float,
    end_sec: float | None,
    hook_line: str,
    thumbnail_dir: str,
    api_key: str,
    model: str,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    ensure_frame: Callable[[str, float], Awaitable[bytes | None]] | None = None,
    extra_ranges: list[tuple[float, float | None]] | None = None,
    avoid_timestamps: list[float] | None = None,
) -> FramePickResult:
    """Harvest candidates → quality filter → Gemini rank → readiness fallback."""
    fid = (drive_file_id or "").strip()
    heuristic = heuristic_frame_ts(start_sec, end_sec)
    heuristic_url = f"/media/video/{fid}/frame?ts={heuristic}" if fid else None
    base = FramePickResult(
        timestamp_sec=heuristic,
        preview_url=heuristic_url,
        frame_source="heuristic",
        instagram_ready=False,
        ranked_timestamps=[heuristic],
    )
    if not fid:
        base.warning = "missing drive_file_id"
        return base

    avoid = [float(x) for x in (avoid_timestamps or [])]
    # Prefer a mid-span heuristic that isn't already used by another slide.
    if avoid and any(abs(heuristic - a) < 0.45 for a in avoid):
        span_end = float(end_sec) if end_sec is not None else start_sec + 2.0
        for frac in (0.25, 0.75, 0.1, 0.9):
            alt = round(float(start_sec) + max(0.0, span_end - float(start_sec)) * frac, 2)
            if not any(abs(alt - a) < 0.45 for a in avoid):
                heuristic = alt
                heuristic_url = f"/media/video/{fid}/frame?ts={heuristic}"
                base = FramePickResult(
                    timestamp_sec=heuristic,
                    preview_url=heuristic_url,
                    frame_source="heuristic",
                    instagram_ready=False,
                    ranked_timestamps=[heuristic],
                )
                break

    # Oversample, then quality-filter down to max_candidates for Gemini.
    harvest_n = min(_HARD_CAP_CANDIDATES, max(int(max_candidates) * 2, int(max_candidates)))
    raw_candidates = build_frame_candidates(
        fid,
        start_sec,
        end_sec,
        max_candidates=harvest_n,
        extra_ranges=extra_ranges,
    )
    if len(raw_candidates) < 2 or not api_key:
        return base

    async def _run() -> FramePickResult:
        raw_images: list[bytes | None] = []
        for c in raw_candidates:
            data = load_cached_frame_bytes(thumbnail_dir, fid, c.timestamp_sec)
            if data is None and ensure_frame is not None:
                try:
                    data = await ensure_frame(fid, c.timestamp_sec)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("ensure_frame failed %s@%.2f: %s", fid, c.timestamp_sec, exc)
                    data = None
            raw_images.append(data)

        if sum(1 for x in raw_images if x) < 1:
            return FramePickResult(
                timestamp_sec=heuristic,
                preview_url=heuristic_url,
                frame_source="heuristic",
                instagram_ready=False,
                ranked_timestamps=[c.timestamp_sec for c in raw_candidates],
                warning="no frame images available",
                quality_stats={"candidates": len(raw_candidates), "kept": 0},
            )

        kept_idx, qstats = filter_frame_candidates_by_quality(
            raw_images,
            timestamps=[c.timestamp_sec for c in raw_candidates],
            max_keep=max(2, min(int(max_candidates), _HARD_CAP_CANDIDATES)),
            min_keep=2,
        )
        candidates: list[FrameCandidate] = []
        images: list[bytes | None] = []
        for new_i, old_i in enumerate(kept_idx):
            c = raw_candidates[old_i]
            if avoid and any(abs(float(c.timestamp_sec) - a) < 0.45 for a in avoid):
                continue
            candidates.append(
                FrameCandidate(
                    index=len(candidates),
                    timestamp_sec=c.timestamp_sec,
                    label=c.label,
                    preview_url=c.preview_url,
                )
            )
            images.append(raw_images[old_i])

        # If avoid wiped everything, fall back to quality-kept without avoid.
        if not candidates and kept_idx:
            for new_i, old_i in enumerate(kept_idx):
                c = raw_candidates[old_i]
                candidates.append(
                    FrameCandidate(
                        index=new_i,
                        timestamp_sec=c.timestamp_sec,
                        label=c.label,
                        preview_url=c.preview_url,
                    )
                )
                images.append(raw_images[old_i])

        # Ensure a heuristic-labeled candidate remains when possible.
        if candidates and not any(c.label == "heuristic" for c in candidates):
            mid_i = min(
                range(len(candidates)),
                key=lambda i: abs(candidates[i].timestamp_sec - heuristic),
            )
            c = candidates[mid_i]
            candidates[mid_i] = FrameCandidate(
                index=c.index,
                timestamp_sec=c.timestamp_sec,
                label="heuristic",
                preview_url=c.preview_url,
            )

        quality_meta = {
            "candidates": len(raw_candidates),
            "kept": len(candidates),
            **qstats,
        }

        if not candidates:
            return FramePickResult(
                timestamp_sec=heuristic,
                preview_url=heuristic_url,
                frame_source="heuristic",
                instagram_ready=False,
                ranked_timestamps=[c.timestamp_sec for c in raw_candidates],
                warning="all candidates failed quality (pixelation/exposure)",
                quality_stats=quality_meta,
            )

        order, ready = await asyncio.to_thread(
            rank_candidates_with_gemini_sync,
            hook_line=hook_line,
            candidates=candidates,
            images=images,
            api_key=api_key,
            model=model,
        )
        if not order:
            # Fall back to best quality-filtered sample (index 0 after quality sort).
            chosen = candidates[0]
            return FramePickResult(
                timestamp_sec=chosen.timestamp_sec,
                preview_url=chosen.preview_url or heuristic_url,
                frame_source="heuristic",
                instagram_ready=False,
                ranked_timestamps=[c.timestamp_sec for c in candidates],
                warning="gemini rank unavailable",
                quality_stats=quality_meta,
            )
        heuristic_index = next(
            (c.index for c in candidates if c.label == "heuristic"),
            0,
        )
        idx, source, ig_ready = pick_ready_from_ranked(
            order=order,
            ready=ready,
            n=len(candidates),
            heuristic_index=heuristic_index,
        )
        chosen = candidates[idx]
        ranked_ts = [
            candidates[i].timestamp_sec
            for i in order
            if 0 <= i < len(candidates)
        ]
        return FramePickResult(
            timestamp_sec=chosen.timestamp_sec,
            preview_url=chosen.preview_url or heuristic_url,
            frame_source=source,
            instagram_ready=ig_ready,
            ranked_timestamps=ranked_ts or [c.timestamp_sec for c in candidates],
            quality_stats=quality_meta,
        )

    try:
        return await asyncio.wait_for(_run(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        base.warning = "frame select timed out"
        return base
    except Exception as exc:  # noqa: BLE001
        logger.warning("select_frame_for_span failed: %s", exc)
        base.warning = "frame select failed"
        return base


async def polish_slides_instagram_frames(
    slides: list[dict[str, Any]],
    *,
    thumbnail_dir: str,
    api_key: str,
    model: str,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    ensure_frame: Callable[[str, float], Awaitable[bytes | None]] | None = None,
    concurrency: int = 2,
    prefer_local: bool = False,
    style_copy_refs: list[str] | None = None,
    style_image_bytes: list[bytes] | None = None,
    max_rank_batches: int = _DEFAULT_MAX_GEMINI_RANK_BATCHES,
    llm_pack: dict[str, Any] | None = None,
    trace_id: str = "-",
    cached_frame_index: dict[str, CachedVideoFrameIndex] | None = None,
) -> list[dict[str, Any]]:
    """Apply frame polish with concurrent harvest and studio-picker ranking.

    Candidate collection is local/concurrent and cache-first. The studio LLM
    sees 4-6 slides per request. The batch count scales with ambiguous slides
    and is capped by ``max_rank_batches``. Confident local rankings skip the
    LLM. Timestamp collisions are resolved after rankings so early slides
    cannot starve later ones.
    """
    from app.llm.carousel_llm import vision_ready

    if not slides:
        logger.info("frame-select trace=%s event=empty_input", trace_id)
        return slides
    if not vision_ready(llm_pack, api_key=api_key):
        logger.info(
            "frame-select trace=%s event=heuristic_only reason=vision_not_configured slides=%d",
            trace_id,
            len(slides),
        )
        for s in slides:
            s.setdefault("frame_source", "heuristic")
            s.setdefault("instagram_ready", False)
            ts = float(s.get("frame_ts") or heuristic_frame_ts(
                float(s.get("timestamp_sec") or 0), s.get("end_timestamp_sec")
            ))
            s.setdefault("frame_ts", ts)
            fx, fy, fs = focal_point_for_slide(s, ts)
            s.setdefault("focal_x", fx)
            s.setdefault("focal_y", fy)
            s.setdefault("front_face_score", fs)
        return slides

    # Keep harvest small: indexer frames + a few samples beat 10 ffmpeg extracts.
    candidate_cap = max(3, min(int(max_candidates), 4))
    cache_keys = [
        _frame_pick_cache_key(
            slide,
            model=model,
            prefer_local=prefer_local,
            max_candidates=candidate_cap,
        )
        for slide in slides
    ]
    cached_payloads = [_candidate_cache_get(key) for key in cache_keys]
    if cached_payloads and all(payload is not None for payload in cached_payloads):
        logger.info(
            "frame-select trace=%s event=candidate_cache_hit slides=%d",
            trace_id,
            len(slides),
        )
        merged: list[dict[str, Any]] = []
        for slide, payload in zip(slides, cached_payloads):
            out = dict(slide)
            out.update(payload or {})
            merged.append(out)
        return merged

    pipeline_started = time.monotonic()
    logger.info(
        "frame-select trace=%s event=start slides=%d candidate_cap=%d prefer_local=%s allow_extracts=%s timeout_sec=%.0f",
        trace_id,
        len(slides),
        candidate_cap,
        prefer_local,
        ensure_frame is not None,
        timeout_sec,
    )
    extract_sem = asyncio.Semaphore(max(1, min(3, int(concurrency or 2))))

    async def _harvest(slide: dict[str, Any]) -> dict[str, Any]:
        out = dict(slide)
        # Human-picked / locked frames must survive re-runs of select-images.
        source = str(out.get("frame_source") or "").strip().lower()
        locked = bool(out.get("frame_locked")) or source == "manual"
        if locked and out.get("frame_ts") is not None:
            try:
                locked_ts = float(out["frame_ts"])
            except (TypeError, ValueError):
                locked_ts = None
            if locked_ts is not None:
                fid = str(out.get("drive_file_id") or "")
                out["frame_ts"] = locked_ts
                out["preview_url"] = (
                    out.get("preview_url")
                    or carousel_frame_preview_url(fid, locked_ts)
                )
                out["frame_source"] = "manual"
                out["instagram_ready"] = True
                fx, fy, fs = focal_point_for_slide(out, locked_ts)
                out["focal_x"] = fx
                out["focal_y"] = fy
                out["front_face_score"] = fs
                existing = out.get("frame_candidate_items")
                if not isinstance(existing, list) or not existing:
                    out["frame_candidate_items"] = [
                        {
                            "frame_ts": round(locked_ts, 3),
                            "preview_url": out["preview_url"],
                            "label": "manual",
                            "order": 0,
                            "quality_score": 0.0,
                            "front_face_score": float(fs or 0.0),
                            "selected": True,
                        }
                    ]
                out["frame_candidates"] = [
                    float(item.get("frame_ts") or locked_ts)
                    for item in out["frame_candidate_items"]
                    if isinstance(item, dict)
                ] or [locked_ts]
                out["frame_quality"] = {
                    "rank_source": "manual",
                    "candidates": len(out["frame_candidates"]),
                    "kept": len(out["frame_candidates"]),
                }
                return {
                    "slide": out,
                    "candidates": [],
                    "images": [],
                    "heuristic": locked_ts,
                    "local_ok": True,
                    "locked": True,
                    "quality": out["frame_quality"],
                }

        start = float(out.get("timestamp_sec") or 0)
        end = out.get("end_timestamp_sec")
        try:
            end_f = float(end) if end is not None else None
        except (TypeError, ValueError):
            end_f = None
        fid = str(out.get("drive_file_id") or "")
        video_frames = (cached_frame_index or {}).get(fid)
        heuristic = heuristic_frame_ts(start, end_f)
        avoid = [
            float(x)
            for x in (out.get("_avoid_timestamps") or out.get("avoid_timestamps") or [])
            if x is not None
        ]
        raw = build_cache_first_candidates(
            fid,
            start,
            end_f,
            thumbnail_dir=thumbnail_dir,
            max_candidates=candidate_cap,
            cached_frames=video_frames,
        )
        if avoid:
            filtered = [
                c
                for c in raw
                if not any(abs(c.timestamp_sec - a) < 0.45 for a in avoid)
            ]
            if filtered:
                raw = [
                    FrameCandidate(
                        index=i,
                        timestamp_sec=c.timestamp_sec,
                        label=c.label,
                        preview_url=c.preview_url,
                    )
                    for i, c in enumerate(filtered)
                ]

        # Cache-first pass (wide nearest so 1s indexer samples hit).
        # Canonicalize each hit to the actual on-disk timestamp — cache_only
        # GETs refuse nearest-neighbour substitution at serve time.
        resolved_rows: list[tuple[FrameCandidate, bytes] | None] = []
        for c in raw:
            resolved = resolve_cached_frame(
                thumbnail_dir,
                fid,
                c.timestamp_sec,
                nearest_tolerance_sec=_HARVEST_NEAREST_TOLERANCE_SEC,
                cached_frames=video_frames,
            )
            if resolved is None:
                resolved_rows.append(None)
                continue
            canon_ts, data = resolved
            resolved_rows.append(
                (
                    FrameCandidate(
                        index=c.index,
                        timestamp_sec=canon_ts,
                        label=c.label,
                        preview_url=carousel_frame_preview_url(fid, canon_ts),
                    ),
                    data,
                )
            )
        images: list[bytes | None] = [
            None if row is None else row[1] for row in resolved_rows
        ]
        # Retarget raw stamps to canonical cache paths before quality/ranking.
        for i, row in enumerate(resolved_rows):
            if row is None:
                continue
            raw[i] = row[0]
        cache_hits = sum(1 for data in images if data is not None)
        miss_order = sorted(
            (i for i, data in enumerate(images) if data is None),
            key=lambda i: (
                0 if raw[i].label == "heuristic" else 1,
                abs(raw[i].timestamp_sec - heuristic),
            ),
        )
        extracts_used = 0
        if ensure_frame is not None and miss_order:
            # Only extract a tiny budget — regenerate/select must not Drive-storm.
            for i in miss_order[:_MAX_EXTRACTS_PER_SLIDE]:
                async with extract_sem:
                    try:
                        data = await ensure_frame(fid, raw[i].timestamp_sec)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "ensure_frame failed %s@%.2f: %s",
                            fid,
                            raw[i].timestamp_sec,
                            exc,
                        )
                        data = None
                if data is not None:
                    # Prefer the exact extracted stem when it landed on disk.
                    extracted = resolve_cached_frame(
                        thumbnail_dir,
                        fid,
                        raw[i].timestamp_sec,
                        nearest_tolerance_sec=0.051,
                        cached_frames=None,
                    )
                    canon_ts = (
                        extracted[0]
                        if extracted is not None
                        else round(float(raw[i].timestamp_sec), 3)
                    )
                    raw[i] = FrameCandidate(
                        index=raw[i].index,
                        timestamp_sec=canon_ts,
                        label=raw[i].label,
                        preview_url=carousel_frame_preview_url(fid, canon_ts),
                    )
                    images[i] = extracted[1] if extracted is not None else data
                    extracts_used += 1

        # Drop unverified rows and collapse duplicates that snapped to one JPEG.
        verified_raw: list[FrameCandidate] = []
        verified_images: list[bytes] = []
        seen_ts: set[float] = set()
        for c, data in zip(raw, images):
            if data is None:
                continue
            key = round(float(c.timestamp_sec), 3)
            if key in seen_ts:
                continue
            seen_ts.add(key)
            verified_raw.append(
                FrameCandidate(
                    index=len(verified_raw),
                    timestamp_sec=key,
                    label=c.label,
                    preview_url=carousel_frame_preview_url(fid, key),
                )
            )
            verified_images.append(data)
        raw = verified_raw
        images = verified_images  # type: ignore[assignment]
        cache_hits = len(verified_images)

        if not raw:
            return {
                "slide": out,
                "candidates": [],
                "images": [],
                "heuristic": heuristic,
                "local_ok": False,
                "locked": False,
                "quality": {
                    "candidates": 0,
                    "kept": 0,
                    "cache_hits": 0,
                    "extracts": extracts_used,
                    "rejected": {"no_cached_frame": 1},
                },
            }

        quality_scores: list[dict[str, Any]] = []
        kept, qstats = await asyncio.to_thread(
            filter_frame_candidates_by_quality,
            images,
            timestamps=[c.timestamp_sec for c in raw],
            max_keep=candidate_cap,
            min_keep=min(2, candidate_cap),
            quality_scores_out=quality_scores,
            # Cached frames were already produced by the indexing pipeline.
            # Interactive select-images must stay responsive; the full
            # multi-signal mosaic detector is reserved for non-local passes.
            check_pixelation=not prefer_local,
        )
        # Tests and third-party callers may replace the filter without filling
        # the optional score output. Keep that compatibility path off-loop too.
        if len(quality_scores) != len(images):
            quality_scores = await asyncio.to_thread(
                lambda: [
                    (
                        score_frame_quality(image)
                        if not prefer_local
                        else score_frame_quality(image, check_pixelation=False)
                    )
                    for image in images
                ]
            )
        candidates: list[FrameCandidate] = []
        kept_images: list[bytes | None] = []
        for old_i in kept:
            c = raw[old_i]
            candidate_quality = quality_scores[old_i]
            candidates.append(
                FrameCandidate(
                    index=len(candidates),
                    timestamp_sec=c.timestamp_sec,
                    label=c.label,
                    preview_url=c.preview_url,
                    quality_score=float(candidate_quality.get("score") or 0.0),
                    front_face=_front_face_for_slide(out, c.timestamp_sec),
                    perceptual_hash=candidate_quality.get("phash"),
                )
            )
            kept_images.append(images[old_i])
        # Face metadata is a local signal: promote front-facing portraits
        # without excluding otherwise usable candidates.
        order = sorted(
            range(len(candidates)),
            key=lambda i: (candidates[i].front_face, candidates[i].quality_score),
            reverse=True,
        )
        if order:
            candidates = [
                FrameCandidate(
                    index=i,
                    timestamp_sec=candidates[j].timestamp_sec,
                    label=candidates[j].label,
                    preview_url=candidates[j].preview_url,
                    quality_score=candidates[j].quality_score,
                    front_face=candidates[j].front_face,
                    perceptual_hash=candidates[j].perceptual_hash,
                )
                for i, j in enumerate(order)
            ]
            kept_images = [kept_images[j] for j in order]
        best_face = max((c.front_face for c in candidates), default=0.0)
        # Prefer local ranking when cache is warm (or caller asked for fast regen).
        local_ok = bool(candidates) and (
            prefer_local
            or (
                extracts_used == 0
                and cache_hits >= 2
                and (
                    best_face >= _LOCAL_RANK_FACE_THRESHOLD
                    or cache_hits >= min(3, candidate_cap)
                )
            )
        )
        return {
            "slide": out,
            "candidates": candidates,
            "images": kept_images,
            "heuristic": heuristic,
            "local_ok": local_ok,
            "locked": False,
            "quality": {
                "candidates": len(raw),
                "kept": len(candidates),
                "cache_hits": cache_hits,
                "extracts": extracts_used,
                **qstats,
            },
        }

    harvest_started = time.monotonic()
    harvested = await asyncio.wait_for(
        asyncio.gather(*[_harvest(slide) for slide in slides]),
        timeout=timeout_sec,
    )
    logger.info(
        "frame-select trace=%s event=harvest_done stage_ms=%d elapsed_ms=%d slides=%d raw_candidates=%d kept_candidates=%d cache_hits=%d extracts=%d local_ok=%d",
        trace_id,
        round((time.monotonic() - harvest_started) * 1000),
        round((time.monotonic() - pipeline_started) * 1000),
        len(harvested),
        sum(int(item["quality"].get("candidates") or 0) for item in harvested),
        sum(len(item["candidates"]) for item in harvested),
        sum(int(item["quality"].get("cache_hits") or 0) for item in harvested),
        sum(int(item["quality"].get("extracts") or 0) for item in harvested),
        sum(1 for item in harvested if item.get("local_ok")),
    )

    groups: list[tuple[int, str, list[FrameCandidate], list[bytes | None]]] = []
    for idx, item in enumerate(harvested):
        if item.get("locked"):
            continue
        if item["candidates"] and not item.get("local_ok"):
            slide = item["slide"]
            hook = str(
                slide.get("transcript_text")
                or slide.get("hook_line")
                or slide.get("snippet")
                or ""
            )
            groups.append((idx, hook, item["candidates"], item["images"]))

    ranked: dict[int, tuple[list[int] | None, list[bool] | None]] = {}
    # Skip Gemini entirely when every slide ranked locally from cache/faces.
    # Four-to-six slides per request; batch count scales with ambiguous slides.
    batch_size = max(4, min(6, int(concurrency or 5)))
    batch_limit = gemini_rank_batch_limit(
        len(groups),
        batch_size,
        max_batches=max_rank_batches,
    )
    batches = [groups[i : i + batch_size] for i in range(0, len(groups), batch_size)]
    batches = batches[:batch_limit]
    gemini_attempted = {idx for batch in batches for idx, *_ in batch}
    logger.info(
        "frame-select trace=%s event=rank_plan ambiguous_slides=%d batches=%d batch_size=%d candidate_images=%d",
        trace_id,
        len(groups),
        len(batches),
        batch_size,
        sum(len(group[2]) for batch in batches for group in batch),
    )
    if batches:
        rank_started = time.monotonic()
        results = await asyncio.gather(
            *[
                asyncio.to_thread(
                    rank_grouped_candidates,
                    groups=batch,
                    llm_pack=llm_pack,
                    api_key=api_key,
                    model=model,
                    style_copy_refs=style_copy_refs,
                    style_image_bytes=style_image_bytes,
                )
                for batch in batches
            ]
        )
        for result in results:
            ranked.update(result)
        logger.info(
            "frame-select trace=%s event=rank_done stage_ms=%d batches=%d ranked_slides=%d",
            trace_id,
            round((time.monotonic() - rank_started) * 1000),
            len(batches),
            len(ranked),
        )
    # Local-ok slides: identity order (already sorted by face+quality).
    for idx, item in enumerate(harvested):
        if item.get("local_ok") and item["candidates"] and idx not in ranked:
            n = len(item["candidates"])
            ranked[idx] = (list(range(n)), [True] * n)

    # Two-phase assignment: reserve top choices only after every slide is ranked.
    assignments: dict[int, tuple[int, str, bool]] = {}
    used: list[float] = []
    previous_hash: str | None = None
    diversity_swaps: set[int] = set()
    for idx, item in enumerate(harvested):
        if item.get("locked"):
            continue
        candidates = item["candidates"]
        if not candidates:
            assignments[idx] = (0, "heuristic", False)
            continue
        order, ready = ranked.get(idx, (None, None))
        heuristic_i = min(
            range(len(candidates)),
            key=lambda i: abs(candidates[i].timestamp_sec - item["heuristic"]),
        )
        ranked_order = list(order or range(len(candidates)))
        if ready and len(ready) == len(candidates):
            ranked_order = [i for i in ranked_order if ready[i]] + [
                i for i in range(len(candidates)) if i not in ranked_order
            ]
        # Local indexed-face evidence overrides a visually plausible but
        # profile/back-of-head Gemini choice when a clearly better portrait
        # exists in this same spoken span.
        best_face_i = max(
            range(len(candidates)),
            key=lambda i: candidates[i].front_face,
        )
        # Hard rule: prefer any center-facing person when one exists.
        if candidates[best_face_i].front_face >= 0.22:
            ranked_order = [best_face_i] + [
                i for i in ranked_order if i != best_face_i
            ]
        elif (
            candidates[best_face_i].front_face >= 0.18
            and candidates[best_face_i].front_face
            > candidates[ranked_order[0]].front_face + 0.08
        ):
            ranked_order = [best_face_i] + [
                i for i in ranked_order if i != best_face_i
            ]
        # Prefer any usable single-face portrait over near-zero face scores
        # (audience / torso) even when Gemini ranked the weak frame first.
        usable_faces = [
            i for i in ranked_order if candidates[i].front_face >= 0.18
        ]
        if usable_faces and candidates[ranked_order[0]].front_face < 0.15:
            ranked_order = usable_faces + [
                i for i in ranked_order if i not in usable_faces
            ]
        # Still fall back to Gemini/quality order when no face signal exists.
        choice = next(
            (
                i
                for i in ranked_order
                if 0 <= i < len(candidates)
                and not any(
                    abs(candidates[i].timestamp_sec - used_ts) < 0.45
                    for used_ts in used
                )
            ),
            None,
        )
        if choice is None:
            choice = heuristic_i
        choice, diversity_swapped = choose_adjacent_diverse_candidate(
            candidates,
            ranked_order,
            choice,
            previous_hash,
        )
        if diversity_swapped:
            diversity_swaps.add(idx)
        if item.get("local_ok"):
            source = "heuristic"
        else:
            source = "ai" if order and choice in order else "heuristic"
        ready_flag = bool(
            item.get("local_ok")
            or (ready and choice < len(ready) and ready[choice])
        )
        assignments[idx] = (choice, source, ready_flag)
        used.append(candidates[choice].timestamp_sec)
        previous_hash = candidates[choice].perceptual_hash

    local_ranked = sum(
        1 for item in harvested if item.get("local_ok") and not item.get("locked")
    )
    gemini_ranked = sum(1 for idx in gemini_attempted if idx in ranked)
    rank_coverage = {
        "ambiguous": len(groups),
        "gemini_attempted": len(gemini_attempted),
        "gemini_ranked": gemini_ranked,
        "local_ranked": local_ranked,
        "unranked_fallback": max(0, len(groups) - gemini_ranked),
        "batch_limit": batch_limit,
        "batch_size": batch_size,
    }

    out_slides: list[dict[str, Any]] = []
    for idx, item in enumerate(harvested):
        out = dict(item["slide"])
        out.pop("_avoid_timestamps", None)
        out.pop("avoid_timestamps", None)
        if item.get("locked"):
            out_slides.append(out)
            continue
        candidates = item["candidates"]
        fid = str(out.get("drive_file_id") or "")
        if candidates:
            choice, source, ready_flag = assignments[idx]
            chosen = candidates[choice]
            ranked_order = list(
                ranked.get(idx, (None, None))[0] or range(len(candidates))
            )
            if not ranked_order:
                ranked_order = list(range(len(candidates)))
            ordered = [choice] + [i for i in ranked_order if i != choice]
            for i in range(len(candidates)):
                if i not in ordered:
                    ordered.append(i)
            ordered = ordered[:_MAX_EMITTED_CANDIDATES]
            # Recommendation only — never auto-mark selected so studio can
            # open text-only and let the user pick explicitly.
            if prefer_local or item.get("local_ok"):
                recommendation_source = "local" if source != "ai" else "ai"
            elif source == "ai":
                recommendation_source = "ai"
            else:
                recommendation_source = "local"
            out["preview_url"] = carousel_frame_preview_url(fid, chosen.timestamp_sec)
            out["frame_ts"] = round(float(chosen.timestamp_sec), 3)
            out["frame_source"] = source
            out["instagram_ready"] = ready_flag
            out["frame_diversity"] = {
                "adjacent_duplicate_avoided": idx in diversity_swaps,
                "phash_available": bool(chosen.perceptual_hash),
            }
            out["frame_candidates"] = [
                round(float(candidates[i].timestamp_sec), 3) for i in ordered
            ]
            out["frame_candidate_items"] = [
                frame_candidate_item(
                    drive_file_id=fid,
                    candidate=candidates[i],
                    order=order_i,
                    selected=False,
                    recommended=(i == choice),
                    recommendation_source=(
                        recommendation_source if i == choice else None
                    ),
                )
                for order_i, i in enumerate(ordered)
            ]
        else:
            # No verified cache-backed JPEG — do not invent a cache_only URL.
            out["frame_ts"] = None
            out["preview_url"] = None
            out["frame_source"] = "heuristic"
            out["instagram_ready"] = False
            out["frame_warning"] = "no frame images available"
            out["frame_candidates"] = []
            out["frame_candidate_items"] = []
        if out.get("frame_ts") is not None:
            focal_x, focal_y, face_score = focal_point_for_slide(
                out, float(out["frame_ts"])
            )
            out["focal_x"] = focal_x
            out["focal_y"] = focal_y
            out["front_face_score"] = face_score
        else:
            out["focal_x"] = out.get("focal_x")
            out["focal_y"] = out.get("focal_y")
            out["front_face_score"] = out.get("front_face_score") or 0.0
        quality = dict(item["quality"] or {})
        quality["rank_coverage"] = rank_coverage
        if prefer_local or item.get("local_ok"):
            quality["rank_source"] = "local"
        elif idx in gemini_attempted:
            quality["rank_source"] = "gemini"
        else:
            quality["rank_source"] = "fallback"
        out["frame_quality"] = quality
        out_slides.append(out)

    for slide, key in zip(out_slides, cache_keys):
        if slide.get("frame_source") == "manual":
            continue
        items = slide.get("frame_candidate_items")
        # Never persist empty harvests — cold cache + extract retries must not
        # keep serving a cached "no frames" miss.
        if not isinstance(items, list) or not items:
            _CANDIDATE_RESULT_CACHE.pop(key, None)
            continue
        _candidate_cache_put(key, _frame_fields_for_cache(slide))

    logger.info(
        "frame-select trace=%s event=done elapsed_ms=%d slides=%d llm_attempted=%d diversity_swaps=%d",
        trace_id,
        round((time.monotonic() - pipeline_started) * 1000),
        len(out_slides),
        len(gemini_attempted),
        len(diversity_swaps),
    )
    return out_slides
