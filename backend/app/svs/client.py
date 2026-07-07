"""
app/svs/client.py
=================
Thin async client for the Semantic Video Search API (POST /api/v1/search).

SVS returns two result types:
  "visual"     — a scene-change frame that matched the query image embedding
  "transcript" — a Whisper segment whose text embedding matched the query

Both are in the same SigLIP embedding space so a single query hits both at once.

Note: Connection: close is set on all requests to avoid Docker port-proxy
keep-alive issues on Windows (the proxy accepts the TCP connection but hangs
when the client tries to reuse it for a second HTTP/1.1 request).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from app.config import get_settings

_NO_KEEPALIVE = {"Connection": "close"}

logger = logging.getLogger(__name__)


@dataclass
class SVSHit:
    """Raw hit from SVS /api/v1/search, normalised for DFI use."""
    score: float
    hit_type: str          # "visual" | "transcript"
    timestamp: float
    timestamp_start: float | None
    timestamp_end: float | None
    text: str | None       # transcript text (transcript hits only)
    filename: str          # original video filename
    drive_file_id: str     # Drive file ID (empty for videos indexed before webhook)
    video_id: str          # SVS-internal UUID (not used by DFI, kept for debug)


async def search_svs(query: str) -> list[SVSHit]:
    """
    Query SVS and return a list of SVSHit objects.

    Returns an empty list on any error so the caller's fallback logic works.
    """
    settings = get_settings()
    if not settings.svs_enabled:
        return []

    user_id = settings.svs_user_id
    if not user_id:
        logger.debug("SVS: svs_user_id not set — skipping SVS search")
        return []

    # SVS search is POST /api/v1/search with multipart/form-data
    url = f"{settings.svs_base_url.rstrip('/')}/api/v1/search"
    form_data = {
        "query":   query,
        "user_id": user_id,
        "limit":   str(settings.svs_result_limit),
    }

    try:
        async with httpx.AsyncClient(timeout=settings.svs_timeout_seconds) as client:
            resp = await client.post(url, data=form_data, headers=_NO_KEEPALIVE)
            resp.raise_for_status()
            raw: list[dict] = resp.json()
    except Exception as exc:
        logger.warning("SVS search failed (query=%r): %s", query, exc)
        return []

    hits: list[SVSHit] = []
    for item in raw:
        ts = item.get("timestamp") or 0.0
        hits.append(SVSHit(
            score=float(item.get("score") or 0.0),
            hit_type=item.get("type", "visual"),
            timestamp=float(ts),
            timestamp_start=_f(item.get("timestamp_start")),
            timestamp_end=_f(item.get("timestamp_end")),
            text=item.get("text"),
            filename=item.get("filename", ""),
            drive_file_id=item.get("drive_file_id", ""),
            video_id=item.get("video_id", ""),
        ))

    logger.info("SVS returned %d hits for query %r", len(hits), query)
    return hits


async def svs_ready() -> bool:
    """Ping SVS /api/v1/health. Returns False on any error."""
    settings = get_settings()
    if not settings.svs_enabled:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{settings.svs_base_url.rstrip('/')}/api/v1/health",
                headers=_NO_KEEPALIVE,
            )
            return resp.status_code == 200
    except Exception:
        return False


def _f(v) -> float | None:
    """Safe float cast, returns None for falsy / non-numeric values."""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
