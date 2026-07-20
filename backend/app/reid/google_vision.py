from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Face


VISION_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
VISION_API_ENABLE_URL = "https://console.cloud.google.com/apis/library/vision.googleapis.com"


class GoogleVisionNotConfigured(RuntimeError):
    """Raised when no key is available for the official Google Vision API."""


class GoogleVisionApiError(RuntimeError):
    """Raised when Google Cloud Vision rejects the configured key/request."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class VisionKey:
    value: str
    source: str


def _vision_key() -> VisionKey | None:
    """Prefer Gemini key — same AI Studio/gen-lang-client project we already use."""
    settings = get_settings()
    candidates = (
        (settings.google_vision_api_key, "GOOGLE_VISION_API_KEY"),
        (settings.gemini_api_key, "GEMINI_API_KEY"),
        (settings.google_api_key, "GOOGLE_API_KEY"),
    )
    for value, source in candidates:
        if value:
            return VisionKey(value=value, source=source)
    return None


def official_image_search_status() -> dict:
    key = _vision_key()
    return {
        "configured": key is not None,
        "key_source": key.source if key else None,
        "api": "Google Cloud Vision Web Detection",
        "endpoint": "vision.googleapis.com/v1/images:annotate",
        "feature": "WEB_DETECTION",
        "scope_required_if_using_oauth": VISION_SCOPE,
        "api_key_scopes": "API keys do not use OAuth scopes; enable Cloud Vision API on the key's Cloud project.",
        "enable_url": VISION_API_ENABLE_URL,
    }


def _parse_vision_results(payload: dict) -> dict:
    responses = payload.get("responses") or []
    response = responses[0] if responses and isinstance(responses[0], dict) else {}
    if response.get("error"):
        error = response["error"]
        raise GoogleVisionApiError(error.get("message") or "Google Vision returned an error")

    web = response.get("webDetection") or {}

    def image_items(name: str) -> list[dict]:
        return [
            {"url": item.get("url"), "score": item.get("score")}
            for item in web.get(name, []) or []
            if isinstance(item, dict) and item.get("url")
        ]

    def page_items() -> list[dict]:
        out: list[dict] = []
        for item in web.get("pagesWithMatchingImages", []) or []:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            out.append(
                {
                    "url": item.get("url"),
                    "page_title": item.get("pageTitle"),
                    "score": item.get("score"),
                    "full_matching_images": image_items_from_page(item, "fullMatchingImages"),
                    "partial_matching_images": image_items_from_page(item, "partialMatchingImages"),
                }
            )
        return out

    return {
        "best_guess_labels": [
            label.get("label")
            for label in web.get("bestGuessLabels", []) or []
            if isinstance(label, dict) and label.get("label")
        ],
        "web_entities": [
            {
                "description": entity.get("description"),
                "entity_id": entity.get("entityId"),
                "score": entity.get("score"),
            }
            for entity in web.get("webEntities", []) or []
            if isinstance(entity, dict) and (entity.get("description") or entity.get("entityId"))
        ],
        "full_matching_images": image_items("fullMatchingImages"),
        "partial_matching_images": image_items("partialMatchingImages"),
        "visually_similar_images": image_items("visuallySimilarImages"),
        "pages_with_matching_images": page_items(),
    }


def image_items_from_page(page: dict, name: str) -> list[dict]:
    return [
        {"url": item.get("url"), "score": item.get("score")}
        for item in page.get(name, []) or []
        if isinstance(item, dict) and item.get("url")
    ]


async def _call_vision(request_image: dict, *, max_results: int) -> dict:
    key = _vision_key()
    if key is None:
        raise GoogleVisionNotConfigured("Set GOOGLE_VISION_API_KEY, or enable Cloud Vision API for GOOGLE_API_KEY/GEMINI_API_KEY.")

    body = {
        "requests": [
            {
                "image": request_image,
                "features": [{"type": "WEB_DETECTION", "maxResults": max(1, min(max_results, 50))}],
            }
        ]
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://vision.googleapis.com/v1/images:annotate",
            params={"key": key.value},
            json=body,
        )

    if response.status_code >= 400:
        try:
            detail = response.json().get("error", {}).get("message") or response.text
        except Exception:  # noqa: BLE001
            detail = response.text
        raise GoogleVisionApiError(detail, status_code=response.status_code)

    parsed = _parse_vision_results(response.json())
    return {**parsed, "provider": "google_cloud_vision_web_detection", "key_source": key.source}


async def official_image_search_by_url(image_url: str, *, max_results: int = 10) -> dict:
    return await _call_vision({"source": {"imageUri": image_url}}, max_results=max_results)


async def official_image_search_face(session: AsyncSession, face_id: int, *, max_results: int = 10) -> dict:
    face = await session.get(Face, face_id)
    if face is None:
        raise ValueError(f"Face {face_id} not found")
    if not face.thumbnail_path:
        raise ValueError(f"Face {face_id} has no thumbnail")

    path = Path(face.thumbnail_path)
    if not path.exists():
        raise ValueError(f"Face {face_id} thumbnail is missing on disk")

    content = base64.b64encode(path.read_bytes()).decode("ascii")
    result = await _call_vision({"content": content}, max_results=max_results)
    return {**result, "face_id": face_id}
