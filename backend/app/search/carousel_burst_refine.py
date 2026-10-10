"""Refine identity-recommended slide frames via a short 4K burst.

Finds an anchor (catalog appearance → low-res probe → span midpoint), extracts
5 native-res frames in one ffmpeg call, scores them, writes the winner to the
canonical ``{ts:.3f}.jpg`` stem plus a ``master/`` copy, and updates the
recommended candidate. Any failure silently leaves the slide unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.search.carousel_frame_select import (
    cached_frame_path,
    carousel_frame_preview_url,
    heuristic_frame_ts,
    pick_best_burst_frame,
)
from app.video.frame_burst import (
    BURST_ALGO_VERSION,
    burst_output_dir,
    cleanup_burst_losers,
    ensure_burst_extracted,
    load_burst_result_cache,
    save_burst_result_cache,
)

logger = logging.getLogger(__name__)

FACE_SIDECAR_SUFFIX = ".face.json"


def face_sidecar_path(frame_path: Path) -> Path:
    return Path(str(frame_path) + FACE_SIDECAR_SUFFIX)


def write_face_sidecar(frame_path: Path, *, focal_x: float, focal_y: float) -> None:
    try:
        face_sidecar_path(frame_path).write_text(
            json.dumps({"focal_x": float(focal_x), "focal_y": float(focal_y)}),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("face sidecar write failed: %s", exc)


def read_face_sidecar(frame_path: Path) -> tuple[float, float] | None:
    path = face_sidecar_path(frame_path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return float(payload["focal_x"]), float(payload["focal_y"])
    except Exception:  # noqa: BLE001
        return None


def master_frame_path(thumbnail_dir: str, drive_file_id: str, ts: float) -> Path:
    return Path(thumbnail_dir) / "video" / drive_file_id / "master" / f"{float(ts):.3f}.jpg"


def anchor_from_identity_catalog(
    catalog: dict[str, Any] | None,
    *,
    association: dict[str, Any] | None,
    start_sec: float,
    end_sec: float | None,
) -> tuple[float, str]:
    """Prefer the speaker's best appearance; else span midpoint.

    Returns ``(anchor_ts, source)`` where source is ``catalog`` | ``heuristic``.
    """
    heuristic = heuristic_frame_ts(start_sec, end_sec)
    if not isinstance(catalog, dict) or not isinstance(association, dict):
        return heuristic, "heuristic"
    mode = str(association.get("mode") or "")
    if mode == "group_panel":
        panel_ts = association.get("panel_frame_ts")
        if panel_ts is not None:
            try:
                return float(panel_ts), "catalog"
            except (TypeError, ValueError):
                pass
    if mode != "speaker":
        return heuristic, "heuristic"
    identity_id = str(association.get("identity_id") or "")
    identities = {
        str(item.get("id")): item
        for item in (catalog.get("identities") or [])
        if isinstance(item, dict) and item.get("id")
    }
    identity = identities.get(identity_id) or {}
    apps = [a for a in (identity.get("appearances") or []) if isinstance(a, dict)]
    if not apps:
        return heuristic, "heuristic"
    best = max(
        apps,
        key=lambda a: (
            float(a.get("front_face_score") or 0),
            float(a.get("quality_score") or 0),
            float(a.get("detection_confidence") or 0),
        ),
    )
    try:
        return float(best["frame_ts"]), "catalog"
    except (TypeError, ValueError, KeyError):
        return heuristic, "heuristic"


def probe_quote_window_anchor(
    source: str,
    *,
    start_sec: float,
    end_sec: float,
    headers: str | None = None,
    timeout_sec: float = 30.0,
) -> float | None:
    """Cheap low-res probe (fps=2, scale=480) over the quote window.

    Returns the timestamp of the best single-clear-face stretch, or None.
    """
    import subprocess
    import tempfile

    import cv2

    start = max(0.0, float(start_sec))
    end = max(start + 0.05, float(end_sec))
    window = end - start
    with tempfile.TemporaryDirectory(prefix="burst-probe-") as tmp:
        pattern = str(Path(tmp) / "p_%03d.jpg")
        cmd: list[str] = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        if headers:
            cmd.extend(
                ["-headers", headers if headers.endswith("\r\n") else f"{headers}\r\n"]
            )
        cmd.extend(
            [
                "-ss",
                f"{start:.3f}",
                "-i",
                str(source),
                "-t",
                f"{window:.3f}",
                "-vf",
                "fps=2,scale=480:-1",
                "-q:v",
                "7",
                pattern,
            ]
        )
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout_sec)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        if proc.returncode != 0:
            return None
        files = sorted(Path(tmp).glob("p_*.jpg"))
        if not files:
            return None
        try:
            from app.faces.engine import get_face_engine

            engine = get_face_engine()
        except Exception:  # noqa: BLE001
            return None
        best_ts: float | None = None
        best_score = -1.0
        for i, path in enumerate(files):
            image = cv2.imread(str(path))
            if image is None or image.size == 0:
                continue
            try:
                faces = list(engine.detect_faces(image))
            except Exception:  # noqa: BLE001
                continue
            if len(faces) != 1:
                continue
            face = faces[0]
            h, w = image.shape[:2]
            area = (float(face.bbox_width) * float(face.bbox_height)) / max(w * h, 1)
            score = float(face.confidence) * 0.5 + min(area, 0.4) * 2.0
            if score > best_score:
                best_score = score
                # fps=2 → midway through each 0.5s bucket
                best_ts = round(start + (i + 0.5) / 2.0, 3)
        return best_ts


def resolve_burst_anchor(
    *,
    catalog: dict[str, Any] | None,
    association: dict[str, Any] | None,
    start_sec: float,
    end_sec: float | None,
    source: str | None = None,
    headers: str | None = None,
    allow_probe: bool = True,
) -> tuple[float, str]:
    """Catalog appearance → optional low-res probe → heuristic midpoint."""
    anchor, src = anchor_from_identity_catalog(
        catalog,
        association=association,
        start_sec=start_sec,
        end_sec=end_sec,
    )
    if src == "catalog":
        return anchor, src
    end = float(end_sec) if end_sec is not None else float(start_sec)
    if allow_probe and source and end > float(start_sec) + 0.2:
        try:
            probed = probe_quote_window_anchor(
                source,
                start_sec=float(start_sec),
                end_sec=end,
                headers=headers,
            )
            if probed is not None:
                return float(probed), "probe"
        except Exception as exc:  # noqa: BLE001
            logger.debug("burst probe failed: %s", exc)
    return heuristic_frame_ts(start_sec, end_sec), "heuristic"


def _copy_winner_artifacts(
    winner_path: Path,
    *,
    thumbnail_dir: str,
    drive_file_id: str,
    winner_ts: float,
    focal_x: float,
    focal_y: float,
) -> Path:
    """Write winner to canonical stem + master; attach face sidecar."""
    canonical = cached_frame_path(thumbnail_dir, drive_file_id, winner_ts)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    master = master_frame_path(thumbnail_dir, drive_file_id, winner_ts)
    master.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(winner_path, canonical)
    shutil.copy2(winner_path, master)
    write_face_sidecar(canonical, focal_x=focal_x, focal_y=focal_y)
    write_face_sidecar(master, focal_x=focal_x, focal_y=focal_y)
    return canonical


async def resolve_video_source_for_burst(
    drive_file_id: str,
    settings: Settings,
    session: Any | None = None,
) -> tuple[str | None, str | None]:
    """Return ``(source, headers)`` — local cache first, then Drive stream URL."""
    try:
        from app.db.models import DriveFile
        from app.video.youtube_cache import video_cache_path
        from app.video.youtube_registry import is_youtube_source

        async def _from_session(db: Any) -> tuple[str | None, str | None]:
            drive_file = await db.get(DriveFile, drive_file_id)
            if drive_file is not None:
                src = video_cache_path(settings, drive_file)
                if src.is_file():
                    return str(src), None
                if is_youtube_source(drive_file):
                    return None, None
            return await _drive_stream_source(drive_file_id, settings, db)

        if session is not None:
            return await _from_session(session)

        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as own:
            return await _from_session(own)
    except Exception as exc:  # noqa: BLE001
        logger.debug("burst source resolve failed: %s", exc)
        return None, None


async def _drive_stream_source(
    drive_file_id: str,
    settings: Settings,
    session: Any,
) -> tuple[str | None, str | None]:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select as sa_select

    from app.auth_credentials import carousel_google
    from app.db.models import DriveUser
    from app.drive.google_client import _do_token_refresh

    if session is None:
        return None, None
    user = (await session.execute(sa_select(DriveUser).limit(1))).scalar_one_or_none()
    if user is None:
        return None, None
    now = datetime.now(tz=timezone.utc)
    if user.token_expiry is None or user.token_expiry - timedelta(minutes=5) <= now:
        if not user.refresh_token:
            return None, None
        try:
            creds = carousel_google(settings)
            new_token, new_expiry = await asyncio.to_thread(
                _do_token_refresh,
                user.refresh_token,
                creds.client_id,
                creds.client_secret,
            )
            user.access_token = new_token
            user.token_expiry = new_expiry
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("burst token refresh failed: %s", exc)
            return None, None
    from urllib.parse import quote

    # The id comes from the request; never let it add path/query segments to a
    # URL we attach a Bearer token to.
    url = f"https://www.googleapis.com/drive/v3/files/{quote(str(drive_file_id), safe='')}?alt=media"
    headers = f"Authorization: Bearer {user.access_token}\r\n"
    return url, headers


async def _preview_for_winner(
    thumbnail_dir: str,
    drive_file_id: str,
    winner_ts: float,
    *,
    prefer_hdr: bool,
) -> tuple[str | None, bool]:
    """Preview URL for a burst winner, keeping the HDR derivative like other candidates."""
    preview = carousel_frame_preview_url(drive_file_id, winner_ts)
    hdr_ok = False
    if prefer_hdr:
        try:
            from app.video.frame_enhance import ensure_hdr_for_timestamp

            result = await asyncio.to_thread(
                ensure_hdr_for_timestamp, thumbnail_dir, drive_file_id, winner_ts
            )
            hdr_ok = bool(result.get("ok"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("burst hdr derivative failed: %s", exc)
    if preview and hdr_ok:
        preview = f"{preview}&variant=hdr"
    return preview, hdr_ok


def _detect_faces_on_paths(paths: list[Path]) -> list[list[Any]]:
    """Best-effort local face detection for burst scoring (one frame at a time)."""
    import cv2

    out: list[list[Any]] = [[] for _ in paths]
    try:
        from app.faces.engine import get_face_engine

        engine = get_face_engine()
    except Exception:  # noqa: BLE001
        return out
    for i, path in enumerate(paths):
        try:
            image = cv2.imread(str(path))
            if image is None or image.size == 0:
                continue
            # Downscale for detection speed; boxes stay in pixel coords of this image.
            h, w = image.shape[:2]
            scale = min(1.0, 960.0 / max(h, w))
            if scale < 1.0:
                image = cv2.resize(
                    image,
                    (max(1, int(w * scale)), max(1, int(h * scale))),
                    interpolation=cv2.INTER_AREA,
                )
            out[i] = list(engine.detect_faces(image))
        except Exception:  # noqa: BLE001
            out[i] = []
    return out


async def refine_recommended_with_burst(
    *,
    thumbnail_dir: str,
    drive_file_id: str,
    start_sec: float,
    end_sec: float | None,
    catalog: dict[str, Any] | None,
    association: dict[str, Any] | None,
    recommended: dict[str, Any],
    settings: Settings | None = None,
    source: str | None = None,
    headers: str | None = None,
    video_duration: float | None = None,
    prefer_hdr: bool = True,
) -> dict[str, Any] | None:
    """Run burst refine for one recommended candidate.

    Returns an updated candidate dict, or None to keep the original.
    Never raises.
    """
    try:
        settings = settings or get_settings()
        if not bool(getattr(settings, "carousel_burst_enabled", True)):
            return None

        end = float(end_sec) if end_sec is not None else float(start_sec)
        # The probe shells out to ffmpeg and runs the face engine: keep it off
        # the event loop so one slow slide cannot stall every other request.
        anchor, anchor_src = await asyncio.to_thread(
            resolve_burst_anchor,
            catalog=catalog,
            association=association,
            start_sec=float(start_sec),
            end_sec=end,
            source=source,
            headers=headers,
            allow_probe=bool(source),
        )

        cached = load_burst_result_cache(thumbnail_dir, drive_file_id, anchor)
        if cached and cached.get("winner_ts") is not None:
            winner_ts = float(cached["winner_ts"])
            canonical = cached_frame_path(thumbnail_dir, drive_file_id, winner_ts)
            if not canonical.is_file():
                cached_path = cached.get("canonical_path")
                if cached_path and Path(str(cached_path)).is_file():
                    canonical = Path(str(cached_path))
            if canonical.is_file():
                updated = dict(recommended)
                updated["frame_ts"] = round(winner_ts, 3)
                preview, hdr_ok = await _preview_for_winner(
                    thumbnail_dir, drive_file_id, winner_ts, prefer_hdr=prefer_hdr
                )
                updated["preview_url"] = preview
                updated["hdr"] = hdr_ok
                updated["recommendation_source"] = "burst"
                updated["burst"] = {
                    "anchor_ts": round(anchor, 3),
                    "anchor_source": anchor_src,
                    "algo_version": BURST_ALGO_VERSION,
                    "cache_hit": True,
                    "score": cached.get("score"),
                }
                if cached.get("focal_x") is not None:
                    updated["focal_x"] = cached.get("focal_x")
                    updated["focal_y"] = cached.get("focal_y")
                return updated

        if not source:
            return None

        n = int(getattr(settings, "carousel_burst_frames", 5) or 5)
        window_sec = float(getattr(settings, "carousel_burst_window_sec", 1.6) or 1.6)
        max_edge = int(getattr(settings, "carousel_burst_max_long_edge", 3840) or 3840)
        out_dir = burst_output_dir(thumbnail_dir, drive_file_id, anchor)
        frames = await ensure_burst_extracted(
            drive_file_id=drive_file_id,
            anchor_ts=anchor,
            source=source,
            out_dir=out_dir,
            window_sec=window_sec,
            n=n,
            headers=headers,
            max_long_edge=max_edge,
            quote_start=float(start_sec),
            quote_end=end,
            duration=video_duration,
        )
        if not frames:
            return None

        faces_by_index = await asyncio.to_thread(
            _detect_faces_on_paths, [p for _ts, p in frames]
        )
        gemini = bool(getattr(settings, "carousel_burst_gemini_tiebreak", False))
        picked = await asyncio.to_thread(
            pick_best_burst_frame,
            frames,
            faces_by_index=faces_by_index,
            gemini_tiebreak=gemini,
        )
        if picked is None:
            cleanup_burst_losers(frames, None)
            return None
        winner_ts, winner_path, quality = picked
        focal_x = float(quality.get("focal_x") or 0.5)
        focal_y = float(quality.get("focal_y") or 0.4)
        canonical = await asyncio.to_thread(
            _copy_winner_artifacts,
            winner_path,
            thumbnail_dir=thumbnail_dir,
            drive_file_id=drive_file_id,
            winner_ts=winner_ts,
            focal_x=focal_x,
            focal_y=focal_y,
        )
        # The winner now lives at the canonical stem and under master/. Drop every
        # burst frame (including the winner's burst copy) instead of keeping a
        # third full-resolution copy per slide.
        master_path = master_frame_path(thumbnail_dir, drive_file_id, winner_ts)
        cleanup_burst_losers(frames, master_path)

        save_burst_result_cache(
            thumbnail_dir,
            drive_file_id,
            anchor,
            {
                "winner_ts": round(float(winner_ts), 3),
                "winner_path": str(master_path),
                "canonical_path": str(canonical),
                "master_path": str(master_path),
                "score": float(quality.get("score") or 0.0),
                "focal_x": focal_x,
                "focal_y": focal_y,
                "anchor_source": anchor_src,
                "frame_count": len(frames),
            },
        )

        updated = dict(recommended)
        updated["frame_ts"] = round(float(winner_ts), 3)
        preview, hdr_ok = await _preview_for_winner(
            thumbnail_dir, drive_file_id, winner_ts, prefer_hdr=prefer_hdr
        )
        updated["preview_url"] = preview
        updated["hdr"] = hdr_ok
        updated["quality_score"] = round(float(quality.get("score") or 0.0), 4)
        updated["front_face_score"] = round(
            float(quality.get("front_face_score") or recommended.get("front_face_score") or 0.0),
            6,
        )
        updated["focal_x"] = focal_x
        updated["focal_y"] = focal_y
        updated["recommendation_source"] = "burst"
        updated["burst"] = {
            "anchor_ts": round(anchor, 3),
            "anchor_source": anchor_src,
            "algo_version": BURST_ALGO_VERSION,
            "cache_hit": False,
            "score": float(quality.get("score") or 0.0),
            "frames": len(frames),
        }
        return updated
    except Exception as exc:  # noqa: BLE001
        logger.debug("burst refine failed: %s", exc)
        return None


async def apply_burst_refine_to_slides(
    slides: list[dict[str, Any]],
    *,
    thumbnail_dir: str,
    drive_file_id: str,
    catalog: dict[str, Any] | None,
    settings: Settings | None = None,
    source: str | None = None,
    headers: str | None = None,
    video_duration: float | None = None,
    deadline_monotonic: float | None = None,
    prefer_hdr: bool = True,
    concurrency: int = 2,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Refine recommended candidates with bursts.

    Slides run with bounded concurrency. The per-request cap and the deadline are
    enforced *during* a slide too (``wait_for`` on the remaining budget), so one
    slow burst cannot push the whole select-images call past its timeout; a slide
    that runs out of time keeps today's single-frame candidates.
    """
    settings = settings or get_settings()
    summary: dict[str, Any] = {
        "enabled": bool(getattr(settings, "carousel_burst_enabled", True)),
        "attempted": 0,
        "succeeded": 0,
        "skipped": 0,
        "cache_hits": 0,
        "algo_version": BURST_ALGO_VERSION,
    }
    if not summary["enabled"]:
        return slides, summary

    max_per = int(getattr(settings, "carousel_burst_max_per_request", 12) or 12)
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    out: list[dict[str, Any]] = [dict(slide) for slide in slides]
    # (slide index, recommended candidate index) for the first ``max_per`` slides.
    work: list[tuple[int, int]] = []
    for idx, item in enumerate(out):
        candidates = list(item.get("frame_candidate_items") or [])
        rec_idx = next(
            (
                i
                for i, c in enumerate(candidates)
                if isinstance(c, dict) and c.get("recommended")
            ),
            None,
        )
        if rec_idx is None:
            continue
        if len(work) >= max_per:
            summary["skipped"] += 1
            continue
        work.append((idx, rec_idx))

    async def _one(idx: int, rec_idx: int) -> dict[str, Any] | None:
        item = out[idx]
        async with sem:
            remaining: float | None = None
            if deadline_monotonic is not None:
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0.5:
                    summary["skipped"] += 1
                    return None
            summary["attempted"] += 1
            association = item.get("identity_association")
            if not isinstance(association, dict):
                association = {}
            candidates = list(item.get("frame_candidate_items") or [])
            coro = refine_recommended_with_burst(
                thumbnail_dir=thumbnail_dir,
                drive_file_id=str(item.get("drive_file_id") or drive_file_id),
                start_sec=float(item.get("timestamp_sec") or 0),
                end_sec=item.get("end_timestamp_sec"),
                catalog=catalog,
                association=association,
                recommended=candidates[rec_idx],
                settings=settings,
                source=source,
                headers=headers,
                video_duration=video_duration,
                prefer_hdr=prefer_hdr,
            )
            try:
                if remaining is not None:
                    return await asyncio.wait_for(coro, timeout=remaining)
                return await coro
            except (asyncio.TimeoutError, TimeoutError):
                summary["skipped"] += 1
                return None
            except Exception as exc:  # noqa: BLE001
                logger.debug("burst slide refine failed: %s", exc)
                return None

    results = await asyncio.gather(*[_one(i, r) for i, r in work])

    for (idx, rec_idx), updated in zip(work, results):
        if updated is None:
            continue
        item = out[idx]
        candidates = list(item.get("frame_candidate_items") or [])
        candidates[rec_idx] = updated
        # Keep frame_candidates timestamps in sync.
        item["frame_candidate_items"] = candidates
        item["frame_candidates"] = [
            float(c["frame_ts"])
            for c in candidates
            if isinstance(c, dict) and c.get("frame_ts") is not None
        ]
        if updated.get("burst", {}).get("cache_hit"):
            summary["cache_hits"] += 1
        summary["succeeded"] += 1
        fq = dict(item.get("frame_quality") or {})
        fq["burst"] = updated.get("burst")
        item["frame_quality"] = fq
    return out, summary
