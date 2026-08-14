"""Live provider model catalog (mocked HTTP — no network)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm import provider_models as pm


@pytest.mark.asyncio
async def test_fetch_openrouter_models_filters_batch_and_maps_rows():
    pm._cache.clear()
    payload = {
        "data": [
            {
                "id": "anthropic/claude-opus-5",
                "name": "Anthropic: Claude Opus 5",
                "architecture": {"modality": "text->text"},
            },
            {
                "id": "anthropic/claude-opus-5:batch",
                "name": "batch",
                "architecture": {"modality": "text->text"},
            },
            {
                "id": "acme/vision-only",
                "name": "Vision",
                "architecture": {"modality": "image->image"},
            },
        ]
    }
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload

    client = AsyncMock()
    client.get.return_value = resp
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with patch("app.llm.provider_models.httpx.AsyncClient", return_value=client):
        rows = await pm.fetch_openrouter_models()

    assert rows == [
        {
            "id": "anthropic/claude-opus-5",
            "label": "Anthropic: Claude Opus 5",
            "provider": "openrouter",
        }
    ]


@pytest.mark.asyncio
async def test_fetch_anthropic_models_uses_display_name():
    pm._cache.clear()
    payload = {
        "data": [
            {"id": "claude-opus-5", "display_name": "Claude Opus 5"},
            {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"},
        ],
        "has_more": False,
    }
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    client = AsyncMock()
    client.get.return_value = resp
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with patch("app.llm.provider_models.httpx.AsyncClient", return_value=client):
        rows = await pm.fetch_anthropic_models(api_key="sk-test")

    assert [r["id"] for r in rows] == ["claude-opus-5", "claude-sonnet-5"]
    assert rows[0]["label"] == "Claude Opus 5 (direct)"
    assert rows[0]["provider"] == "claude"


@pytest.mark.asyncio
async def test_fetch_gemini_models_skips_tts_and_image():
    pm._cache.clear()
    payload = {
        "models": [
            {
                "name": "models/gemini-3.7-flash",
                "displayName": "Gemini 3.7 Flash",
                "supportedGenerationMethods": ["generateContent"],
            },
            {
                "name": "models/gemini-2.5-flash-preview-tts",
                "displayName": "Gemini 2.5 Flash Preview TTS",
                "supportedGenerationMethods": ["generateContent"],
            },
            {
                "name": "models/gemini-3.1-flash-image",
                "displayName": "Nano Banana 2",
                "supportedGenerationMethods": ["generateContent"],
            },
        ]
    }
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    client = AsyncMock()
    client.get.return_value = resp
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with patch("app.llm.provider_models.httpx.AsyncClient", return_value=client):
        rows = await pm.fetch_gemini_models(api_key="gk-test")

    assert rows == [
        {
            "id": "gemini-3.7-flash",
            "label": "Gemini 3.7 Flash",
            "provider": "gemini",
        }
    ]
