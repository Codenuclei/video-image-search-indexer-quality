from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.config import get_settings
from app.fennec.client import get_fennec_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fennec", tags=["fennec"])


@router.get("/status")
async def fennec_status() -> dict:
    settings = get_settings()
    client = get_fennec_client()
    ready = await client.ready() if settings.fennec_enabled else False
    return {
        "enabled": settings.fennec_enabled,
        "base_url": settings.fennec_base_url,
        "ready": ready,
        "video_cache_dir": settings.fennec_video_cache_dir,
        "ui_url": "http://localhost:8080",
    }


@router.get("/thumbnail/{scene_id}")
async def fennec_thumbnail(scene_id: int) -> Response:
    settings = get_settings()
    if not settings.fennec_enabled:
        raise HTTPException(status_code=503, detail="Fennec is disabled")
    url = f"{settings.fennec_base_url.rstrip('/')}/api/thumbnail/{scene_id}"
    async with httpx.AsyncClient(timeout=settings.fennec_timeout_seconds) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail="Thumbnail not found")
        return Response(
            content=resp.content,
            media_type=resp.headers.get("content-type", "image/jpeg"),
        )


@router.get("/video/{file_id}")
async def fennec_video_proxy(file_id: int, request: Request) -> StreamingResponse:
    settings = get_settings()
    if not settings.fennec_enabled:
        raise HTTPException(status_code=503, detail="Fennec is disabled")
    url = f"{settings.fennec_base_url.rstrip('/')}/api/video/{file_id}"
    headers = {}
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code not in (200, 206):
            raise HTTPException(status_code=resp.status_code, detail="Video not found")
        return StreamingResponse(
            resp.aiter_bytes(),
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "video/mp4"),
            headers={
                k: v
                for k, v in resp.headers.items()
                if k.lower() in ("content-range", "accept-ranges", "content-length")
            },
        )
