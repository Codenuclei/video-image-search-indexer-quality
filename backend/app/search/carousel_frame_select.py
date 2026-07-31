"""Instagram-ready frame selection for carousel slides.

Pipeline per slide (spoken span stays fixed; only the display frame changes):
  1. Sample candidate timestamps across start_sec–end_sec (include heuristic mid-span).
  2. Load JPEG bytes (cache, nearest on disk, optional on-demand extract).
  3. Gemini ranks candidates for Instagram carousel polish.
  4. Gemini readiness flags; walk ranked order until a ready frame (else top / heuristic).
"""

from __future__ import annotations

import asyncio
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
_HARD_CAP_CANDIDATES = 16


@dataclass(frozen=True)
class FrameCandidate:
    index: int
    timestamp_sec: float
    label: str  # "heuristic" | "sample"
    preview_url: str | None = None
    quality_score: float = 0.0
    front_face: float = 0.0


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


def score_frame_quality(jpeg_bytes: bytes | None) -> dict[str, Any]:
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


def filter_frame_candidates_by_quality(
    images: list[bytes | None],
    *,
    timestamps: list[float] | None = None,
    max_keep: int = DEFAULT_MAX_CANDIDATES,
    min_keep: int = 2,
) -> tuple[list[int], dict[str, Any]]:
    """Filter/rank candidate indices by cheap image quality + phash dedupe.

    Returns ``(kept_indices_in_quality_order, reject_stats)``.
    """
    n = len(images)
    reject_counts: dict[str, int] = {}
    scored: list[tuple[float, int, str | None]] = []  # score, idx, phash
    for i, img in enumerate(images):
        q = score_frame_quality(img)
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
        for i, img in enumerate(images):
            q = score_frame_quality(img)
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
    # Normalized boxes are what the index stores. A border-touching face is
    # unusable for a portrait crop even when its detector confidence is high.
    edge_penalty = 1.0
    if x <= 0.015 or y <= 0.015 or x + value("bbox_width", value("width", 0.0)) >= 0.985:
        edge_penalty *= 0.35
    if y + value("bbox_height", value("height", 0.0)) >= 0.985:
        edge_penalty *= 0.5
    area = max(
        0.0,
        value("bbox_width", value("width", 0.0))
        * value("bbox_height", value("height", 0.0)),
    )
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
    return round((pose * 0.65 + confidence * 0.2 + min(area, 0.5) * 0.3) * edge_penalty, 6)


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
                if abs(float(face_ts) - timestamp_sec) > 0.8:
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
    """Return the best indexed/heuristic face score for a candidate timestamp."""
    raw = slide.get("faces") or slide.get("face_detections") or slide.get("frame_faces")
    if isinstance(raw, dict):
        # Indexed payloads commonly use either timestamp keys or a faces list.
        raw = raw.get(str(round(timestamp_sec, 2))) or raw.get(round(timestamp_sec, 2))
    if not isinstance(raw, list):
        raw = [raw] if raw else []
    scored: list[float] = []
    for face in raw:
        if not isinstance(face, dict):
            scored.append(front_face_score(face))
            continue
        face_ts = face.get(
            "timestamp_sec", face.get("frame_timestamp", face.get("ts"))
        )
        if face_ts is not None:
            try:
                if abs(float(face_ts) - timestamp_sec) > 0.8:
                    continue
            except (TypeError, ValueError):
                pass
        # InsightFace landmarks can expose yaw as pose_yaw or head_pose_yaw.
        scored.append(front_face_score(face))
    return max(scored, default=0.0)


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
) -> str:
    parts = [
        "Rank candidates for multiple Instagram carousel slides. "
        "Return ONLY JSON with a slides array. Each item must contain "
        "slide (group index), order (best to worst), and ready flags.",
    ]
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


def rank_grouped_candidates_with_gemini_sync(
    *,
    groups: list[tuple[int, str, list[FrameCandidate], list[bytes | None]]],
    api_key: str,
    model: str,
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
    parts: list = [types.Part(text=_group_rank_prompt(prompt_groups))]
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
        "- clear subject / speaker face when speaking\n"
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


def load_cached_frame_bytes(
    thumbnail_dir: str,
    drive_file_id: str,
    ts: float,
    *,
    nearest_tolerance_sec: float = _NEAREST_TOLERANCE_SEC,
) -> bytes | None:
    """Load exact or nearest cached JPEG under the video frames dir."""
    exact = cached_frame_path(thumbnail_dir, drive_file_id, ts)
    if exact.is_file():
        data = exact.read_bytes()
        if data and len(data) <= _MAX_JPEG_BYTES:
            return data
    frames_dir = exact.parent
    if not frames_dir.is_dir():
        return None
    best: Path | None = None
    best_dist = float("inf")
    for p in frames_dir.glob("*.jpg"):
        try:
            dist = abs(float(p.stem) - ts)
        except ValueError:
            continue
        if dist < best_dist:
            best_dist = dist
            best = p
    if best is not None and best_dist <= nearest_tolerance_sec:
        data = best.read_bytes()
        if data and len(data) <= _MAX_JPEG_BYTES:
            return data
    return None


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
) -> list[dict[str, Any]]:
    """Apply frame polish with concurrent harvest and grouped Gemini ranking.

    Candidate collection is local/concurrent. Gemini sees 4-6 slides per
    request (capped at three calls), and timestamp collisions are resolved
    after all rankings are available so an early slide cannot starve later
    slides.
    """
    if not slides:
        return slides
    if not api_key:
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

    candidate_cap = max(3, min(int(max_candidates), 5))

    async def _harvest(slide: dict[str, Any]) -> dict[str, Any]:
        out = dict(slide)
        start = float(out.get("timestamp_sec") or 0)
        end = out.get("end_timestamp_sec")
        try:
            end_f = float(end) if end is not None else None
        except (TypeError, ValueError):
            end_f = None
        fid = str(out.get("drive_file_id") or "")
        heuristic = heuristic_frame_ts(start, end_f)
        raw = build_frame_candidates(
            fid,
            start,
            end_f,
            max_candidates=min(_HARD_CAP_CANDIDATES, candidate_cap * 2),
        )

        async def _load(candidate: FrameCandidate) -> bytes | None:
            data = load_cached_frame_bytes(thumbnail_dir, fid, candidate.timestamp_sec)
            if data is None and ensure_frame is not None:
                try:
                    data = await ensure_frame(fid, candidate.timestamp_sec)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "ensure_frame failed %s@%.2f: %s",
                        fid,
                        candidate.timestamp_sec,
                        exc,
                    )
            return data

        images = list(await asyncio.gather(*[_load(c) for c in raw]))
        kept, qstats = await asyncio.to_thread(
            filter_frame_candidates_by_quality,
            images,
            timestamps=[c.timestamp_sec for c in raw],
            max_keep=candidate_cap,
            min_keep=min(3, candidate_cap),
        )
        candidates: list[FrameCandidate] = []
        kept_images: list[bytes | None] = []
        for old_i in kept:
            c = raw[old_i]
            candidates.append(
                FrameCandidate(
                    index=len(candidates),
                    timestamp_sec=c.timestamp_sec,
                    label=c.label,
                    preview_url=c.preview_url,
                    front_face=_front_face_for_slide(out, c.timestamp_sec),
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
                )
                for i, j in enumerate(order)
            ]
            kept_images = [kept_images[j] for j in order]
        return {
            "slide": out,
            "candidates": candidates,
            "images": kept_images,
            "heuristic": heuristic,
            "quality": {"candidates": len(raw), "kept": len(candidates), **qstats},
        }

    harvested = await asyncio.wait_for(
        asyncio.gather(*[_harvest(slide) for slide in slides]),
        timeout=timeout_sec,
    )

    groups: list[tuple[int, str, list[FrameCandidate], list[bytes | None]]] = []
    for idx, item in enumerate(harvested):
        if item["candidates"]:
            slide = item["slide"]
            hook = str(
                slide.get("transcript_text")
                or slide.get("hook_line")
                or slide.get("snippet")
                or ""
            )
            groups.append((idx, hook, item["candidates"], item["images"]))

    ranked: dict[int, tuple[list[int] | None, list[bool] | None]] = {}
    # Four-to-six slides per request, with an explicit hard cap of three.
    batch_size = max(4, min(6, int(concurrency or 5)))
    batches = [groups[i : i + batch_size] for i in range(0, len(groups), batch_size)]
    batches = batches[:3]
    if batches:
        results = await asyncio.gather(
            *[
                asyncio.to_thread(
                    rank_grouped_candidates_with_gemini_sync,
                    groups=batch,
                    api_key=api_key,
                    model=model,
                )
                for batch in batches
            ]
        )
        for result in results:
            ranked.update(result)

    # Two-phase assignment: reserve top choices only after every slide is ranked.
    assignments: dict[int, tuple[int, str, bool]] = {}
    used: list[float] = []
    for idx, item in enumerate(harvested):
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
        if (
            candidates[best_face_i].front_face >= 0.35
            and candidates[best_face_i].front_face
            > candidates[ranked_order[0]].front_face + 0.15
        ):
            ranked_order = [best_face_i] + [
                i for i in ranked_order if i != best_face_i
            ]
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
        source = "ai" if order and choice in order else "heuristic"
        ready_flag = bool(ready and choice < len(ready) and ready[choice])
        assignments[idx] = (choice, source, ready_flag)
        used.append(candidates[choice].timestamp_sec)

    out_slides: list[dict[str, Any]] = []
    for idx, item in enumerate(harvested):
        out = dict(item["slide"])
        candidates = item["candidates"]
        if candidates:
            choice, source, ready_flag = assignments[idx]
            chosen = candidates[choice]
            out["preview_url"] = chosen.preview_url
            out["frame_ts"] = chosen.timestamp_sec
            out["frame_source"] = source
            out["instagram_ready"] = ready_flag
            out["frame_candidates"] = [
                candidates[i].timestamp_sec
                for i in (ranked.get(idx, (None, None))[0] or range(len(candidates)))
            ][:16]
        else:
            ts = item["heuristic"]
            fid = str(out.get("drive_file_id") or "")
            out["frame_ts"] = ts
            out["preview_url"] = (
                f"/media/video/{fid}/frame?ts={ts}&cache_only=1" if fid else None
            )
            out["frame_source"] = "heuristic"
            out["instagram_ready"] = False
            out["frame_warning"] = "no frame images available"
        focal_x, focal_y, face_score = focal_point_for_slide(out, float(out["frame_ts"]))
        out["focal_x"] = focal_x
        out["focal_y"] = focal_y
        out["front_face_score"] = face_score
        out["frame_quality"] = item["quality"]
        out_slides.append(out)
    return out_slides
