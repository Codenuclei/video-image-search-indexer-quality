"""External face crawl MVP — download public image URLs and match into pgvector."""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlparse

import httpx

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Browser-like fetch headers (aligned with face_search._FETCH_HEADERS).
_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; DriveFaceIndexer/1.0; +https://mastersunion.org)"
    ),
    "Accept": "image/*,*/*;q=0.8",
}

_ERR_DOWNLOAD = "Couldn't download this URL"
_ERR_NOT_IMAGE = "This link is a web page, not an image"
_ERR_NO_FACE = "No face detected in this image"
_ERR_TOO_LARGE = "Image is too large (max 12MB)"
_MAX_IMAGE_BYTES = 12 * 1024 * 1024

_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_IMAGE_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    re.IGNORECASE,
)


def unwrap_image_page_url(url: str) -> str:
    """Pull the real image URL from image-search page wrappers (Yahoo/Bing/Google).

    Reads ``imgurl`` / ``mediaurl`` query params (URL-decoded). Returns *url*
    unchanged when no embedded image param is present.
    """
    raw = (url or "").strip()
    if not raw:
        return raw
    try:
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)
        for key in ("imgurl", "mediaurl"):
            values = qs.get(key) or []
            if not values:
                continue
            # parse_qs already URL-decodes; avoid a second unquote that would
            # turn legitimate %20 in paths into spaces.
            candidate = (values[0] or "").strip()
            if candidate.startswith(("http://", "https://")):
                return candidate
    except Exception:  # noqa: BLE001
        pass
    return raw


def extract_og_image_url(html: str) -> str | None:
    """Return the first ``og:image`` meta URL from HTML, if any."""
    if not html:
        return None
    for pattern in (_OG_IMAGE_RE, _OG_IMAGE_RE_ALT):
        match = pattern.search(html)
        if not match:
            continue
        candidate = unquote(match.group(1).strip())
        if candidate.startswith(("http://", "https://")):
            return candidate
    return None


def _looks_like_image_url(url: str) -> bool:
    return url.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


def _is_image_content_type(ctype: str) -> bool:
    lower = (ctype or "").lower()
    return "image" in lower or "octet-stream" in lower


def _is_html_content_type(ctype: str) -> bool:
    lower = (ctype or "").lower()
    return "text/html" in lower or "application/xhtml" in lower


async def _fetch_image_bytes(client: httpx.AsyncClient, url: str) -> tuple[bytes | None, str | None]:
    """Download *url*, unwrapping search-page wrappers and og:image fallbacks.

    Returns ``(bytes, None)`` on success or ``(None, friendly_error)`` on failure.
    """
    target = unwrap_image_page_url(url)
    try:
        resp = await client.get(target, headers=_FETCH_HEADERS)
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        return None, _ERR_DOWNLOAD

    ctype = (resp.headers.get("content-type") or "").lower()
    body = resp.content

    if _is_image_content_type(ctype) or (_looks_like_image_url(target) and not _is_html_content_type(ctype)):
        if not body:
            return None, _ERR_DOWNLOAD
        return body, None

    if _is_html_content_type(ctype) or (body[:200].lstrip().lower().startswith((b"<!doctype", b"<html"))):
        try:
            html = body.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return None, _ERR_NOT_IMAGE
        og = extract_og_image_url(html)
        if not og:
            return None, _ERR_NOT_IMAGE
        try:
            og_resp = await client.get(og, headers=_FETCH_HEADERS)
            og_resp.raise_for_status()
        except Exception:  # noqa: BLE001
            return None, _ERR_DOWNLOAD
        og_ctype = (og_resp.headers.get("content-type") or "").lower()
        if not _is_image_content_type(og_ctype) and not _looks_like_image_url(og):
            return None, _ERR_NOT_IMAGE
        if not og_resp.content:
            return None, _ERR_DOWNLOAD
        return og_resp.content, None

    return None, _ERR_NOT_IMAGE


async def crawl_image_urls(session: "AsyncSession", urls: list[str]) -> dict:
    from app.reid.face_search import search_faces_by_image_bytes

    results: list[dict] = []
    async with httpx.AsyncClient(timeout=45.0, follow_redirects=True, headers=_FETCH_HEADERS) as client:
        for url in urls:
            item: dict = {"url": url, "ok": False}
            try:
                image_bytes, err = await _fetch_image_bytes(client, url)
                if err or image_bytes is None:
                    item["error"] = err or _ERR_DOWNLOAD
                    results.append(item)
                    continue
                if len(image_bytes) > _MAX_IMAGE_BYTES:
                    item["error"] = _ERR_TOO_LARGE
                    results.append(item)
                    continue
                match = await search_faces_by_image_bytes(session, image_bytes, limit=10)
                if int(match.get("faces_detected") or 0) == 0:
                    item["error"] = _ERR_NO_FACE
                    results.append(item)
                    continue
                item["ok"] = True
                item["search"] = match
            except Exception as exc:  # noqa: BLE001
                logger.warning("external crawl failed for %s: %s", url[:80], exc)
                item["error"] = _ERR_DOWNLOAD
            results.append(item)
    return {"crawled": len(results), "results": results}
