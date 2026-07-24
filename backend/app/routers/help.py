"""LLM-powered how-to help for in-app guidance."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/help", tags=["help"])

_CANNED: dict[str, str] = {
    "folders": (
        "1. Connect Google Drive on Folders.\n"
        "2. Add YouTube links if needed (Download to library & index).\n"
        "3. Wait for Pending to drop in the Indexer summary.\n"
        "4. Open Search or Video Carousel to find moments."
    ),
    "search": (
        "1. Type a visual or transcript query.\n"
        "2. Optionally filter by person or mime type.\n"
        "3. Click a moment/image to preview; use Download to save."
    ),
    "search/carousel": (
        "1. Pick a recent or search any captioned video (optional person filter).\n"
        "2. If a person is set, we only check they appear in that video — themes stay normal.\n"
        "3. Choose a theme, then select hooks/topics from the transcript.\n"
        "4. Review preview markers + intent, then generate carousel slides."
    ),
    "library": "Browse indexed media by folder. Filter by status to find errors or pending files.",
    "default": (
        "Use Folders to connect Drive / add YouTube, wait for indexing, "
        "then Search or Video Carousel to find people and moments."
    ),
}


class HowToRequest(BaseModel):
    page: str = Field(default="", max_length=120)
    question: str = Field(default="", max_length=500)


@router.post("/howto")
async def howto(body: HowToRequest) -> dict[str, object]:
    page = (body.page or "").strip().lstrip("/") or "default"
    question = (body.question or "").strip()
    canned_key = page if page in _CANNED else "default"
    canned = _CANNED[canned_key]

    settings = get_settings()
    if not settings.gemini_api_key or not question:
        return {"source": "canned", "page": page, "answer": canned}

    try:
        import google.generativeai as genai

        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel(settings.gemini_model or "gemini-2.5-flash")
        prompt = (
            "You are the in-app help for DriveFaceIndexer (face + video search over Google Drive / YouTube).\n"
            f"User is on page: /{page}\n"
            f"Question: {question}\n"
            "Reply with 3-6 short numbered steps. No marketing fluff."
        )
        result = await __import__("asyncio").to_thread(model.generate_content, prompt)
        text = (getattr(result, "text", None) or "").strip()
        if not text:
            return {"source": "canned", "page": page, "answer": canned}
        return {"source": "llm", "page": page, "answer": text}
    except Exception as exc:  # noqa: BLE001
        logger.warning("howto LLM failed: %s", exc)
        return {"source": "canned", "page": page, "answer": canned, "warning": str(exc)[:160]}
