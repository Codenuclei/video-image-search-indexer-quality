from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_SAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_COOKIES_OPERATOR_HINT = (
    "YouTube requires authentication cookies on the server. "
    "Export cookies for youtube.com in Netscape format, then set either "
    "YTDLP_COOKIES_FILE (or YOUTUBE_COOKIES_FILE) to a path on the Railway volume, "
    "or paste the file contents into the YTDLP_COOKIES (or YOUTUBE_COOKIES) env var. "
    "Redeploy/restart the backend after updating."
)

_BOT_CHECK_MARKERS = (
    "sign in to confirm you're not a bot",
    "sign in to confirm you are not a bot",
    "confirm you're not a bot",
    "cookies are no longer valid",
    "http error 401",
    "http error 403",
)


def _safe_filename(title: str, video_id: str, ext: str) -> str:
    clean = _SAFE_CHARS.sub("", title).strip() or f"YouTube {video_id}"
    clean = clean[:180]
    return f"{clean} [{video_id}].{ext}"


def _looks_like_netscape_cookies(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped:
        return False
    if stripped.startswith("# Netscape") or stripped.startswith("# HTTP Cookie File"):
        return True
    # Tab-separated cookie lines: domain \t flag \t path \t secure \t expiry \t name \t value
    for line in stripped.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and ("youtube" in parts[0].lower() or parts[0].startswith(".")):
            return True
    return False


def _cookie_paths_from_settings(settings: Settings) -> list[str]:
    return [
        p.strip()
        for p in (
            settings.ytdlp_cookies_file,
            settings.youtube_cookies_file,
            os.environ.get("YTDLP_COOKIES_FILE", ""),
            os.environ.get("YOUTUBE_COOKIES_FILE", ""),
        )
        if p and p.strip()
    ]


def _cookie_contents_from_settings(settings: Settings) -> str:
    for value in (
        settings.ytdlp_cookies,
        settings.youtube_cookies,
        os.environ.get("YTDLP_COOKIES", ""),
        os.environ.get("YOUTUBE_COOKIES", ""),
    ):
        if value and value.strip():
            return value
    return ""


def resolve_youtube_cookies_file(settings: Settings | None = None) -> str | None:
    """
    Resolve a Netscape cookies file path for yt-dlp --cookies.

    Prefers an existing path on disk (volume), otherwise writes env cookie
    contents to ``{temp_dir}/youtube_cookies.txt``.
    """
    settings = settings or get_settings()

    for path_str in _cookie_paths_from_settings(settings):
        path = Path(path_str)
        if path.is_file() and path.stat().st_size > 0:
            logger.info("Using yt-dlp cookies file: %s", path)
            return str(path)
        logger.warning("Configured YouTube cookies path missing or empty: %s", path_str)

    contents = _cookie_contents_from_settings(settings)
    if not contents.strip():
        return None

    if not _looks_like_netscape_cookies(contents):
        raise RuntimeError(
            "YOUTUBE_COOKIES / YTDLP_COOKIES does not look like a Netscape cookies file. "
            + _COOKIES_OPERATOR_HINT
        )

    out_dir = Path(settings.temp_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "youtube_cookies.txt"
    # Normalize escaped newlines sometimes pasted into Railway env UIs.
    normalized = contents.replace("\\n", "\n").replace("\\r", "")
    out_path.write_text(normalized, encoding="utf-8")
    logger.info("Wrote yt-dlp cookies from env to %s (%d bytes)", out_path, out_path.stat().st_size)
    return str(out_path)


def require_youtube_cookies_file(settings: Settings | None = None) -> str:
    """Return cookies path or raise a UI-facing operator error."""
    settings = settings or get_settings()
    try:
        path = resolve_youtube_cookies_file(settings)
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError(
            f"Could not prepare YouTube cookies file: {exc}. {_COOKIES_OPERATOR_HINT}"
        ) from exc

    if not path:
        raise RuntimeError(
            "YouTube download blocked: no cookies configured. " + _COOKIES_OPERATOR_HINT
        )
    return path


def prepare_youtube_cookies_at_startup() -> None:
    """Log cookies readiness during app lifespan (non-fatal if missing)."""
    settings = get_settings()
    try:
        path = resolve_youtube_cookies_file(settings)
    except Exception as exc:  # noqa: BLE001
        logger.error("YouTube cookies misconfigured: %s", exc)
        return
    if path:
        logger.info("YouTube yt-dlp cookies ready: %s", path)
    else:
        logger.warning(
            "YouTube downloads will fail until cookies are set. %s",
            _COOKIES_OPERATOR_HINT,
        )


def _rewrite_ytdlp_error(err: str, video_id: str) -> str:
    lower = err.lower()
    if any(m in lower for m in _BOT_CHECK_MARKERS) or "cookies" in lower and "bot" in lower:
        return (
            f"yt-dlp download failed for {video_id}: YouTube bot check / invalid cookies. "
            + _COOKIES_OPERATOR_HINT
        )
    return f"yt-dlp download failed for {video_id}: {err}"


def download_youtube_video_sync(video_id: str, *, title: str | None = None) -> tuple[str, str]:
    """
    Download a YouTube video to a temp file via yt-dlp.

    Requires Netscape cookies (file path or env contents) on server/Railway.

    Returns (local_path, filename).
    """
    settings = get_settings()
    cookies_file = require_youtube_cookies_file(settings)

    tmp_root = Path(settings.temp_dir) / "youtube_downloads"
    tmp_root.mkdir(parents=True, exist_ok=True)

    out_dir = tempfile.mkdtemp(prefix=f"yt-{video_id}-", dir=tmp_root)
    out_template = str(Path(out_dir) / f"%(title)s [{video_id}].%(ext)s")
    url = f"https://www.youtube.com/watch?v={video_id}"

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--cookies",
        cookies_file,
        "-f",
        "bestvideo[ext=webm]+bestaudio[ext=webm]/bestvideo+bestaudio/best",
        "--merge-output-format",
        "webm",
        "-o",
        out_template,
        url,
    ]
    logger.info("Downloading YouTube video %s (cookies=%s)", video_id, cookies_file)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "yt-dlp failed")[:2000]
        raise RuntimeError(_rewrite_ytdlp_error(err, video_id))

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
