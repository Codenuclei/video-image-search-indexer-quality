"""Live model catalogs from OpenRouter / Anthropic / Gemini APIs.

Static lists go stale; the picker should show what the provider currently returns.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 300.0
_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}

# OpenRouter returns hundreds of IDs; keep chat-capable text models, drop batch variants.
_OPENROUTER_SKIP_SUFFIXES = (":batch", ":floor")


def _cached(key: str) -> list[dict[str, str]] | None:
    hit = _cache.get(key)
    if not hit:
        return None
    ts, rows = hit
    if time.monotonic() - ts > _CACHE_TTL_SEC:
        return None
    return rows


def _store(key: str, rows: list[dict[str, str]]) -> list[dict[str, str]]:
    _cache[key] = (time.monotonic(), rows)
    return rows


def _label_from_id(model_id: str) -> str:
    # anthropic/claude-sonnet-4.5 → Claude Sonnet 4.5
    leaf = model_id.split("/")[-1]
    leaf = leaf.replace(":free", " (free)")
    parts = leaf.replace("_", "-").split("-")
    return " ".join(p[:1].upper() + p[1:] for p in parts if p)


async def fetch_openrouter_models(
    *,
    api_key: str = "",
    base_url: str = "https://openrouter.ai/api/v1",
) -> list[dict[str, str]]:
    cached = _cached("openrouter")
    if cached is not None:
        return cached

    url = f"{base_url.rstrip('/')}/models"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if (api_key or "").strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenRouter models fetch failed: %s", str(exc)[:200])
        return []

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []

    rows: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid or any(mid.endswith(suf) for suf in _OPENROUTER_SKIP_SUFFIXES):
            continue
        arch = item.get("architecture") if isinstance(item.get("architecture"), dict) else {}
        modality = str((arch or {}).get("modality") or "")
        # Prefer text-output models (text->text, text+image->text, …).
        if modality and "->text" not in modality and modality != "text":
            continue
        name = str(item.get("name") or "").strip() or _label_from_id(mid)
        rows.append({"id": mid, "label": name, "provider": "openrouter"})

    rows.sort(key=lambda r: r["label"].lower())
    return _store("openrouter", rows)


async def fetch_anthropic_models(*, api_key: str) -> list[dict[str, str]]:
    key = (api_key or "").strip()
    if not key:
        return []
    cached = _cached("anthropic")
    if cached is not None:
        return cached

    rows: list[dict[str, str]] = []
    after: str | None = None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                params: dict[str, Any] = {"limit": 100}
                if after:
                    params["after_id"] = after
                resp = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={
                        "x-api-key": key,
                        "anthropic-version": "2023-06-01",
                    },
                    params=params,
                )
                resp.raise_for_status()
                payload = resp.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, list) or not data:
                    break
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    mid = str(item.get("id") or "").strip()
                    if not mid:
                        continue
                    label = str(item.get("display_name") or "").strip() or _label_from_id(mid)
                    rows.append(
                        {
                            "id": mid,
                            "label": f"{label} (direct)",
                            "provider": "claude",
                        }
                    )
                if not payload.get("has_more"):
                    break
                after = payload.get("last_id") or data[-1].get("id")
                if not after:
                    break
    except Exception as exc:  # noqa: BLE001
        logger.warning("Anthropic models fetch failed: %s", str(exc)[:200])
        return []

    return _store("anthropic", rows)


async def fetch_gemini_models(*, api_key: str) -> list[dict[str, str]]:
    key = (api_key or "").strip()
    if not key:
        return []
    cached = _cached("gemini")
    if cached is not None:
        return cached

    rows: list[dict[str, str]] = []
    page_token: str | None = None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                params: dict[str, Any] = {"key": key, "pageSize": 100}
                if page_token:
                    params["pageToken"] = page_token
                resp = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params=params,
                )
                resp.raise_for_status()
                payload = resp.json()
                models = payload.get("models") if isinstance(payload, dict) else None
                if not isinstance(models, list):
                    break
                for item in models:
                    if not isinstance(item, dict):
                        continue
                    methods = item.get("supportedGenerationMethods") or []
                    if "generateContent" not in methods:
                        continue
                    name = str(item.get("name") or "").strip()
                    mid = name.replace("models/", "") if name else ""
                    if not mid:
                        continue
                    # Skip non-text carousel LLMs (TTS / image / music / AQA).
                    low = mid.lower()
                    skip_bits = (
                        "embedding",
                        "aqa",
                        "tts",
                        "image",
                        "lyria",
                        "nano-banana",
                        "imagen",
                        "veo",
                    )
                    if any(bit in low for bit in skip_bits):
                        continue
                    display = str(item.get("displayName") or "").strip().lower()
                    if any(bit in display for bit in ("tts", "image", "lyria", "nano banana")):
                        continue
                    label = str(item.get("displayName") or "").strip() or _label_from_id(mid)
                    rows.append({"id": mid, "label": label, "provider": "gemini"})
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gemini models fetch failed: %s", str(exc)[:200])
        return []

    return _store("gemini", rows)


async def fetch_live_carousel_model_options() -> list[dict[str, str]]:
    """Query configured providers; fall back to empty lists per provider on failure."""
    settings = get_settings()
    claude_key = (
        settings.anthropic_api_key or settings.claude_api_key or ""
    ).strip()
    openrouter_key = (settings.openrouter_api_key or "").strip()
    gemini_key = (settings.gemini_api_key or "").strip()
    openrouter_base = (
        settings.openrouter_base_url or "https://openrouter.ai/api/v1"
    ).strip()

    claude_rows = await fetch_anthropic_models(api_key=claude_key)
    openrouter_rows = await fetch_openrouter_models(
        api_key=openrouter_key,
        base_url=openrouter_base,
    )
    gemini_rows = await fetch_gemini_models(api_key=gemini_key)
    return [*claude_rows, *openrouter_rows, *gemini_rows]
