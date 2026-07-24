"""External face crawl MVP — download public image URLs and match into pgvector."""
from __future__ import annotations

import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.reid.face_search import search_faces_by_image_bytes

logger = logging.getLogger(__name__)


async def crawl_image_urls(session: AsyncSession, urls: list[str]) -> dict:
    results: list[dict] = []
    async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
        for url in urls:
            item: dict = {"url": url, "ok": False}
            try:
                resp = await client.get(url, headers={"User-Agent": "DriveFaceIndexer/1.0"})
                resp.raise_for_status()
                ctype = (resp.headers.get("content-type") or "").lower()
                if "image" not in ctype and not url.lower().endswith(
                    (".jpg", ".jpeg", ".png", ".webp", ".gif")
                ):
                    item["error"] = f"Not an image content-type: {ctype or 'unknown'}"
                    results.append(item)
                    continue
                match = await search_faces_by_image_bytes(session, resp.content, limit=10)
                item["ok"] = True
                item["search"] = match
            except Exception as exc:  # noqa: BLE001
                logger.warning("external crawl failed for %s: %s", url, exc)
                item["error"] = str(exc)[:300]
            results.append(item)
    return {"crawled": len(results), "results": results}
