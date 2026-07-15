from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

_SAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(title: str, video_id: str, ext: str) -> str:
    clean = _SAFE_CHARS.sub("", title).strip() or f"YouTube {video_id}"
    clean = clean[:180]
    return f"{clean} [{video_id}].{ext}"


def download_youtube_video_sync(video_id: str, *, title: str | None = None) -> tuple[str, str]:
    """
    Download a YouTube video to a temp file via yt-dlp.

    Returns (local_path, filename).
    """
    settings = get_settings()
    tmp_root = Path(settings.temp_dir) / "youtube_downloads"
    tmp_root.mkdir(parents=True, exist_ok=True)

    out_dir = tempfile.mkdtemp(prefix=f"yt-{video_id}-", dir=tmp_root)
    out_template = str(Path(out_dir) / f"%(title)s [{video_id}].%(ext)s")
    url = f"https://www.youtube.com/watch?v={video_id}"

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "-f",
        "bestvideo[ext=webm]+bestaudio[ext=webm]/bestvideo+bestaudio/best",
        "--merge-output-format",
        "webm",
        "-o",
        out_template,
        url,
    ]
    logger.info("Downloading YouTube video %s", video_id)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "yt-dlp failed")[:2000]
        raise RuntimeError(f"yt-dlp download failed for {video_id}: {err}")

    candidates = list(Path(out_dir).glob(f"*[{video_id}].*"))
    if not candidates:
        candidates = list(Path(out_dir).glob("*"))
    if not candidates:
        raise RuntimeError(f"yt-dlp produced no output file for {video_id}")

    local_path = str(candidates[0])
    ext = candidates[0].suffix.lstrip(".") or "webm"
    filename = _safe_filename(title or candidates[0].stem, video_id, ext)
    size_mb = os.path.getsize(local_path) / (1024 * 1024)
    logger.info("Downloaded YouTube %s -> %s (%.1f MB)", video_id, filename, size_mb)
    return local_path, filename
