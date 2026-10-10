"""Windowed multi-frame burst extraction for carousel stills.

One ffmpeg call seeks into a short window around an anchor timestamp, writes
``n`` native-resolution JPEGs, and returns exact per-frame pts from ``showinfo``.
Never decodes the full video. Callers fall back to single-frame extract on ``[]``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BURST_ALGO_VERSION = "burst-v1"
_SHOWINFO_PTS_RE = re.compile(r"pts_time:(?P<pts>-?\d+(?:\.\d+)?)")
_DEFAULT_TIMEOUT_SEC = 45.0
_BURST_JOBS: dict[str, asyncio.Future[list[tuple[float, Path]]]] = {}
_BURST_JOBS_LOCK: asyncio.Lock | None = None


def clamp_burst_window(
    anchor_ts: float,
    window_sec: float,
    *,
    quote_start: float | None = None,
    quote_end: float | None = None,
    duration: float | None = None,
) -> tuple[float, float]:
    """Return ``(start, clamped_window)`` centred on *anchor_ts* when possible.

    The window is clamped to the quote span and the video duration so ffmpeg
    never seeks past known bounds.
    """
    window = max(0.05, float(window_sec))
    anchor = max(0.0, float(anchor_ts))
    half = window / 2.0
    start = anchor - half
    end = anchor + half

    q0 = float(quote_start) if quote_start is not None else None
    q1 = float(quote_end) if quote_end is not None else None
    if q0 is not None and q1 is not None and q1 < q0:
        q0, q1 = q1, q0
    # The quote span only constrains the window when the anchor lies inside it.
    # The speaker's best appearance (identity catalog) is often in a *different*
    # part of the video; clamping to an unrelated quote would collapse the window
    # to a few ms (near-identical frames) and move it away from the anchor.
    if q0 is not None and q1 is not None:
        slack = 0.25
        if anchor < q0 - slack or anchor > q1 + slack:
            q0 = None
            q1 = None
    if q0 is not None:
        start = max(start, q0)
    if q1 is not None:
        end = min(end, q1)

    start = max(0.0, start)
    if duration is not None and duration > 0:
        end = min(end, float(duration))
        start = min(start, max(0.0, float(duration) - 0.05))

    if end <= start:
        # Degenerate quote / duration — emit a tiny window at the clamped start.
        end = start + min(window, 0.2)

    clamped = max(0.05, end - start)
    # If clamping shrank the window from one side only, try to recover length
    # toward the other side without leaving the quote/duration bounds.
    if clamped + 1e-6 < window:
        deficit = window - clamped
        # Prefer extending toward the end first.
        room_end = None
        if q1 is not None:
            room_end = max(0.0, q1 - end)
        if duration is not None and duration > 0:
            dur_room = max(0.0, float(duration) - end)
            room_end = dur_room if room_end is None else min(room_end, dur_room)
        if room_end and room_end > 0:
            take = min(deficit, room_end)
            end += take
            deficit -= take
        if deficit > 1e-9:
            room_start = start - (q0 if q0 is not None else 0.0)
            room_start = max(0.0, room_start)
            take = min(deficit, room_start)
            start -= take
        start = max(0.0, start)
        if duration is not None and duration > 0:
            end = min(end, float(duration))
        if q0 is not None:
            start = max(start, q0)
        if q1 is not None:
            end = min(end, q1)
        clamped = max(0.05, end - start)

    return round(start, 3), round(clamped, 3)


def parse_showinfo_pts(stderr: str | bytes) -> list[float]:
    """Extract ``pts_time`` values from ffmpeg ``showinfo`` stderr."""
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, (bytes, bytearray)) else str(stderr or "")
    out: list[float] = []
    for match in _SHOWINFO_PTS_RE.finditer(text):
        try:
            out.append(float(match.group("pts")))
        except (TypeError, ValueError):
            continue
    return out


def burst_output_dir(thumbnail_dir: str, drive_file_id: str, anchor_ts: float) -> Path:
    return (
        Path(thumbnail_dir)
        / "video"
        / drive_file_id
        / "burst"
        / f"{float(anchor_ts):.3f}"
    )


def burst_result_cache_path(
    thumbnail_dir: str,
    drive_file_id: str,
    anchor_ts: float,
    *,
    algo_version: str = BURST_ALGO_VERSION,
) -> Path:
    return (
        Path(thumbnail_dir)
        / "video"
        / drive_file_id
        / "burst"
        / f"{float(anchor_ts):.3f}"
        / f"result-{algo_version}.json"
    )


def load_burst_result_cache(
    thumbnail_dir: str,
    drive_file_id: str,
    anchor_ts: float,
    *,
    algo_version: str = BURST_ALGO_VERSION,
) -> dict[str, Any] | None:
    path = burst_result_cache_path(
        thumbnail_dir, drive_file_id, anchor_ts, algo_version=algo_version
    )
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("algo_version") or "") != algo_version:
        return None
    winner_ts = payload.get("winner_ts")
    winner_path = payload.get("winner_path")
    if winner_ts is None:
        return None
    if winner_path and not Path(str(winner_path)).is_file():
        # Canonical stem may still exist even if the burst dir was cleaned.
        canonical = payload.get("canonical_path")
        if not canonical or not Path(str(canonical)).is_file():
            return None
    return payload


def save_burst_result_cache(
    thumbnail_dir: str,
    drive_file_id: str,
    anchor_ts: float,
    payload: dict[str, Any],
    *,
    algo_version: str = BURST_ALGO_VERSION,
) -> None:
    path = burst_result_cache_path(
        thumbnail_dir, drive_file_id, anchor_ts, algo_version=algo_version
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = dict(payload)
        body["algo_version"] = algo_version
        partial = path.with_suffix(".partial.json")
        partial.write_text(json.dumps(body), encoding="utf-8")
        partial.replace(path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("burst result cache write failed: %s", exc)


def _burst_vf(n: int, window_sec: float, max_long_edge: int) -> str:
    fps = max(0.1, float(n) / max(0.05, float(window_sec)))
    # Cap long edge; do NOT crop to 1080x1350 — keep native aspect for scoring.
    scale = (
        f"scale='min({int(max_long_edge)},iw)':'min({int(max_long_edge)},ih)'"
        f":force_original_aspect_ratio=decrease"
    )
    return f"fps={fps:.6f},{scale},showinfo"


def extract_burst(
    source: str,
    anchor_ts: float,
    *,
    window_sec: float = 1.6,
    n: int = 5,
    headers: str | None = None,
    out_dir: Path | str | None = None,
    max_long_edge: int = 3840,
    quote_start: float | None = None,
    quote_end: float | None = None,
    duration: float | None = None,
    timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
) -> list[tuple[float, Path]]:
    """Extract ``n`` native-res frames around *anchor_ts* in one ffmpeg call.

    Returns ``[(absolute_timestamp, path), ...]`` or ``[]`` on any failure.
    """
    try:
        n = max(1, int(n))
        window_sec = float(window_sec)
        start, clamped_window = clamp_burst_window(
            float(anchor_ts),
            window_sec,
            quote_start=quote_start,
            quote_end=quote_end,
            duration=duration,
        )
        if out_dir is not None:
            dest = Path(out_dir)
        else:
            import tempfile

            # Never default to a shared fixed path: the next step wipes ``dest``.
            dest = Path(tempfile.mkdtemp(prefix="burst-"))
        if dest.exists():
            # Fresh dir per run so leftover losers never pollute scoring.
            try:
                shutil.rmtree(dest)
            except Exception:  # noqa: BLE001
                pass
        dest.mkdir(parents=True, exist_ok=True)
        pattern = str(dest / "frame_%02d.jpg")
        vf = _burst_vf(n, clamped_window, int(max_long_edge))
        cmd: list[str] = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info"]
        if headers:
            cmd.extend(["-headers", headers if headers.endswith("\r\n") else f"{headers}\r\n"])
        cmd.extend(
            [
                "-ss",
                f"{start:.3f}",
                "-i",
                str(source),
                "-t",
                f"{clamped_window:.3f}",
                "-vf",
                vf,
                "-frames:v",
                str(n),
                "-q:v",
                "2",
                pattern,
            ]
        )
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=max(5.0, float(timeout_sec)),
        )
        if proc.returncode != 0:
            logger.debug(
                "burst ffmpeg failed rc=%s stderr=%s",
                proc.returncode,
                (proc.stderr or b"")[-400:].decode(errors="replace"),
            )
            return []
        pts_list = parse_showinfo_pts(proc.stderr or b"")
        files = sorted(dest.glob("frame_*.jpg"))
        if not files:
            return []
        results: list[tuple[float, Path]] = []
        for i, path in enumerate(files[:n]):
            if not path.is_file() or path.stat().st_size < 32:
                continue
            if i < len(pts_list):
                # showinfo pts_time is relative to the decoded stream after -ss.
                abs_ts = round(start + float(pts_list[i]), 3)
            else:
                # Even spacing fallback when showinfo is missing.
                frac = (i + 0.5) / max(len(files), 1)
                abs_ts = round(start + clamped_window * frac, 3)
            results.append((abs_ts, path))
        return results
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError) as exc:
        logger.debug("burst extract failed: %s", exc)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.debug("burst extract unexpected error: %s", exc)
        return []


def _burst_jobs_lock() -> asyncio.Lock:
    global _BURST_JOBS_LOCK
    if _BURST_JOBS_LOCK is None:
        _BURST_JOBS_LOCK = asyncio.Lock()
    return _BURST_JOBS_LOCK


def burst_job_key(drive_file_id: str, anchor_ts: float) -> str:
    return f"{drive_file_id}:{round(float(anchor_ts), 3):.3f}:burst"


async def ensure_burst_extracted(
    *,
    drive_file_id: str,
    anchor_ts: float,
    source: str,
    out_dir: Path,
    window_sec: float = 1.6,
    n: int = 5,
    headers: str | None = None,
    max_long_edge: int = 3840,
    quote_start: float | None = None,
    quote_end: float | None = None,
    duration: float | None = None,
    timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    extract_sem: asyncio.Semaphore | None = None,
) -> list[tuple[float, Path]]:
    """Coalesce concurrent burst requests for the same ``(fid, anchor)``."""
    key = burst_job_key(drive_file_id, anchor_ts)
    lock = _burst_jobs_lock()
    async with lock:
        fut = _BURST_JOBS.get(key)
        if fut is None:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            _BURST_JOBS[key] = fut

            async def _run() -> None:
                try:
                    sem = extract_sem
                    if sem is None:
                        # Lazy import to reuse media's concurrency cap.
                        from app.routers.media import _EXTRACT_SEM

                        sem = _EXTRACT_SEM

                    async def _call() -> list[tuple[float, Path]]:
                        return await asyncio.to_thread(
                            extract_burst,
                            source,
                            float(anchor_ts),
                            window_sec=window_sec,
                            n=n,
                            headers=headers,
                            out_dir=out_dir,
                            max_long_edge=max_long_edge,
                            quote_start=quote_start,
                            quote_end=quote_end,
                            duration=duration,
                            timeout_sec=timeout_sec,
                        )

                    async with sem:
                        result = await _call()
                    if not fut.done():
                        fut.set_result(result)
                except Exception as exc:  # noqa: BLE001
                    if not fut.done():
                        fut.set_result([])
                    logger.debug("burst coalesced job error: %s", exc)
                finally:
                    _BURST_JOBS.pop(key, None)

            asyncio.create_task(_run())
    return await asyncio.shield(fut)


def cleanup_burst_losers(
    frames: list[tuple[float, Path]],
    winner_path: Path | None,
) -> None:
    """Delete non-winning burst JPEGs after scoring."""
    if winner_path is None:
        return
    try:
        winner_resolved = winner_path.resolve()
    except Exception:  # noqa: BLE001
        winner_resolved = winner_path
    for _ts, path in frames:
        try:
            if path.resolve() == winner_resolved:
                continue
        except Exception:  # noqa: BLE001
            if path == winner_path:
                continue
        try:
            if path.is_file():
                path.unlink()
        except Exception:  # noqa: BLE001
            pass
