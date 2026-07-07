from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings
from app.qwen.vlm import qwen_vlm_ready_sync

router = APIRouter(prefix="/qwen-vlm", tags=["qwen-vlm"])


@router.get("/status")
async def qwen_vlm_status() -> dict:
    settings = get_settings()
    ready = qwen_vlm_ready_sync() if settings.qwen_vlm_enabled else False
    return {
        "enabled": settings.qwen_vlm_enabled,
        "base_url": settings.qwen_vlm_base_url,
        "model": settings.qwen_vlm_model,
        "ready": ready,
    }
