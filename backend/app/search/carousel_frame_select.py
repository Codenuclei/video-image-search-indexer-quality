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

DEFAULT_MAX_CANDIDATES = 6
DEFAULT_TIMEOUT_SEC = 28.0
_MAX_JPEG_BYTES = 512 * 1024
_DOWNSCALE_MAX_DIM = 640
_NEAREST_TOLERANCE_SEC = 1.25


@dataclass(frozen=True)
class FrameCandidate:
    index: int
    timestamp_sec: float
    label: str  # "heuristic" | "sample"
    preview_url: str | None = None


@dataclass
class FramePickResult:
    timestamp_sec: float
    preview_url: str | None
    frame_source: str  # "ai" | "heuristic" | "fallback"
    instagram_ready: bool
    ranked_timestamps: list[float] = field(default_factory=list)
    warning: str | None = None


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

    Prefer start / 25% / mid / 75% / end when the window is short; for longer
    windows, also sample about every ``step_sec`` (default 0.75s) then cap.
    Always includes the heuristic mid-span timestamp.
    """
    s = float(start_sec or 0.0)
    e = float(end_sec) if end_sec is not None else s
    if e < s:
        e = s
    dur = e - s
    heuristic = heuristic_frame_ts(s, e if end_sec is not None else None)
    cap = max(1, min(int(max_candidates), 8))

    if dur < 0.05:
        return [round(s, 2)]

    fractions = (0.0, 0.25, 0.5, 0.75, 1.0)
    points = [round(s + dur * f, 2) for f in fractions]

    interval = float(step_sec) if step_sec is not None else 0.75
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


def build_frame_candidates(
    drive_file_id: str,
    start_sec: float,
    end_sec: float | None,
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> list[FrameCandidate]:
    """Build labeled candidates; heuristic mid-span is marked ``heuristic``."""
    fid = (drive_file_id or "").strip()
    heuristic = heuristic_frame_ts(start_sec, end_sec)
    stamps = sample_candidate_timestamps(start_sec, end_sec, max_candidates=max_candidates)
    out: list[FrameCandidate] = []
    for i, ts in enumerate(stamps):
        label = "heuristic" if abs(ts - heuristic) < 0.011 else "sample"
        url = f"/media/video/{fid}/frame?ts={ts}" if fid else None
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
) -> FramePickResult:
    """Harvest candidates → Gemini rank → readiness fallback for one slide span."""
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

    candidates = build_frame_candidates(
        fid, start_sec, end_sec, max_candidates=max_candidates
    )
    if len(candidates) < 2 or not api_key:
        return base

    async def _run() -> FramePickResult:
        images: list[bytes | None] = []
        for c in candidates:
            data = load_cached_frame_bytes(thumbnail_dir, fid, c.timestamp_sec)
            if data is None and ensure_frame is not None:
                try:
                    data = await ensure_frame(fid, c.timestamp_sec)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("ensure_frame failed %s@%.2f: %s", fid, c.timestamp_sec, exc)
                    data = None
            images.append(data)

        if sum(1 for x in images if x) < 1:
            out = FramePickResult(
                timestamp_sec=heuristic,
                preview_url=heuristic_url,
                frame_source="heuristic",
                instagram_ready=False,
                ranked_timestamps=[c.timestamp_sec for c in candidates],
                warning="no frame images available",
            )
            return out

        order, ready = await asyncio.to_thread(
            rank_candidates_with_gemini_sync,
            hook_line=hook_line,
            candidates=candidates,
            images=images,
            api_key=api_key,
            model=model,
        )
        if not order:
            return FramePickResult(
                timestamp_sec=heuristic,
                preview_url=heuristic_url,
                frame_source="heuristic",
                instagram_ready=False,
                ranked_timestamps=[c.timestamp_sec for c in candidates],
                warning="gemini rank unavailable",
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
    """Apply Instagram frame polish to outline slides (mutates copies)."""
    if not slides:
        return slides
    if not api_key:
        for s in slides:
            s.setdefault("frame_source", "heuristic")
            s.setdefault("instagram_ready", False)
        return slides

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(slide: dict[str, Any]) -> dict[str, Any]:
        out = dict(slide)
        start = float(out.get("timestamp_sec") or 0)
        end = out.get("end_timestamp_sec")
        try:
            end_f = float(end) if end is not None else None
        except (TypeError, ValueError):
            end_f = None
        fid = str(out.get("drive_file_id") or "")
        hook = str(out.get("hook_line") or out.get("snippet") or "")
        async with sem:
            pick = await select_frame_for_span(
                drive_file_id=fid,
                start_sec=start,
                end_sec=end_f,
                hook_line=hook,
                thumbnail_dir=thumbnail_dir,
                api_key=api_key,
                model=model,
                max_candidates=max_candidates,
                timeout_sec=timeout_sec,
                ensure_frame=ensure_frame,
            )
        out["preview_url"] = pick.preview_url
        out["frame_ts"] = pick.timestamp_sec
        out["frame_source"] = pick.frame_source
        out["instagram_ready"] = pick.instagram_ready
        if pick.ranked_timestamps:
            out["frame_candidates"] = pick.ranked_timestamps[:8]
        if pick.warning:
            out["frame_warning"] = pick.warning
        return out

    return list(await asyncio.gather(*(_one(s) for s in slides)))
