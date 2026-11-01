"""Cheap structured LLM helpers for startup catalog chores (Grok via OpenRouter)."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import get_settings
from app.llm import openrouter

logger = logging.getLogger(__name__)

DEFAULT_STARTUP_LLM_MODEL = "x-ai/grok-4-fast"


def startup_llm_model() -> str:
    settings = get_settings()
    return (settings.startup_llm_model or DEFAULT_STARTUP_LLM_MODEL).strip()


async def complete_startup_json(
    prompt: str,
    *,
    system: str = "Return ONLY valid JSON.",
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> dict[str, Any]:
    """Structured JSON via OpenRouter using STARTUP_LLM_MODEL (Grok by default)."""
    settings = get_settings()
    key = (settings.openrouter_api_key or "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is empty")
    raw = await openrouter.complete_json(
        prompt,
        model=startup_llm_model(),
        api_key=key,
        base_url=settings.openrouter_base_url,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError("startup LLM returned non-object JSON")
    return data


async def generate_aliases(
    *,
    name: str,
    existing: list[str] | None = None,
) -> list[str]:
    """Ask Grok for extra search aliases; fall back to heuristics on failure."""
    seed = [a.strip() for a in (existing or []) if a and a.strip()]
    try:
        data = await complete_startup_json(
            (
                f'Startup display name: {name!r}\n'
                f"Existing aliases: {json.dumps(seed)}\n"
                "Return JSON object {\"aliases\": [\"...\"]} with 3-12 lowercase "
                "search aliases (no duplicates, include spaced and compacted forms)."
            ),
            system="You normalize startup brand names into search aliases. Return ONLY JSON.",
        )
        aliases = data.get("aliases") or []
        if isinstance(aliases, list):
            out: list[str] = []
            seen: set[str] = set()
            for item in [*seed, *aliases]:
                if not isinstance(item, str):
                    continue
                key = item.strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(key)
            return out
    except Exception:  # noqa: BLE001
        logger.info("startup alias LLM fallback for %s", name, exc_info=True)
    return _heuristic_aliases(name, seed)


def _heuristic_aliases(name: str, seed: list[str]) -> list[str]:
    import re

    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        key = value.strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        out.append(key)

    for item in seed:
        add(item)
    add(name)
    compact = re.sub(r"[^a-z0-9]+", "", name.lower())
    spaced = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    dashed = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    add(compact)
    add(spaced)
    add(dashed)
    return out
