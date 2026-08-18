"""Resolve carousel LLM provider + credentials for extract/themes/polish.

Default when unset/auto + Anthropic key present: **Claude direct**.
User picks in ``/test`` (Claude / OpenRouter / Gemini) are honored as-is.
"""

from __future__ import annotations

from typing import Any, TypedDict

from app.config import get_settings
from app.runtime_settings import get_runtime_settings

CAROUSEL_LLM_PROVIDERS = frozenset({"auto", "openrouter", "claude", "gemini"})

# Newest stable Sonnet for Anthropic Messages API (themes/topics/hooks default).
# Prefer live /v1/models via provider_models; this is emergency fallback only.
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5-20250929"

CAROUSEL_LLM_PROVIDER_OPTIONS: list[dict[str, str]] = [
    {"id": "claude", "label": "Claude (direct)"},
    {"id": "openrouter", "label": "OpenRouter"},
    {"id": "gemini", "label": "Gemini"},
    {"id": "auto", "label": "Auto"},
]

# Emergency fallbacks only — picker prefers live provider catalogs.
CLAUDE_DIRECT_MODEL_OPTIONS: list[dict[str, str]] = [
    {"id": "claude-opus-5", "label": "Claude Opus 5 (direct)", "provider": "claude"},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5 (direct)", "provider": "claude"},
    {
        "id": "claude-sonnet-4-5-20250929",
        "label": "Claude Sonnet 4.5 (direct)",
        "provider": "claude",
    },
]

OPENROUTER_MODEL_OPTIONS: list[dict[str, str]] = [
    {"id": "anthropic/claude-sonnet-4.5", "label": "Claude Sonnet 4.5", "provider": "openrouter"},
    {"id": "anthropic/claude-sonnet-4", "label": "Claude Sonnet 4", "provider": "openrouter"},
    {"id": "google/gemini-2.5-pro", "label": "Gemini 2.5 Pro", "provider": "openrouter"},
    {"id": "openai/gpt-4.1", "label": "GPT-4.1", "provider": "openrouter"},
]

GEMINI_MODEL_OPTIONS: list[dict[str, str]] = [
    {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "provider": "gemini"},
    {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "provider": "gemini"},
    {"id": "gemini-3.7-flash", "label": "Gemini 3.7 Flash", "provider": "gemini"},
]

CAROUSEL_LLM_MODEL_OPTIONS: list[dict[str, str]] = [
    *CLAUDE_DIRECT_MODEL_OPTIONS,
    *OPENROUTER_MODEL_OPTIONS,
    *GEMINI_MODEL_OPTIONS,
]


class CarouselLlmKwargs(TypedDict):
    provider: str
    api_key: str
    model: str
    claude_api_key: str
    claude_model: str
    openrouter_api_key: str
    openrouter_model: str
    openrouter_base_url: str


def normalize_carousel_llm_provider(value: str | None) -> str:
    raw = (value or "auto").strip().lower()
    return raw if raw in CAROUSEL_LLM_PROVIDERS else "auto"


def claude_configured() -> bool:
    settings = get_settings()
    return bool(
        (settings.anthropic_api_key or "").strip()
        or (settings.claude_api_key or "").strip()
    )


def resolve_claude_model() -> str:
    """Effective Anthropic model id (runtime override → env → default)."""
    settings = get_settings()
    runtime = get_runtime_settings()
    for candidate in (
        (getattr(runtime, "claude_model", None) or "").strip(),
        (settings.claude_model or "").strip(),
    ):
        if candidate:
            return candidate
    return DEFAULT_CLAUDE_MODEL


def effective_carousel_provider(raw: str | None = None) -> str:
    """User selection, with auto → Claude when Anthropic key is present."""
    runtime = get_runtime_settings()
    pref = normalize_carousel_llm_provider(
        raw if raw is not None else runtime.carousel_llm_provider
    )
    if pref == "auto" and claude_configured():
        return "claude"
    if pref == "auto":
        if openrouter_configured():
            return "openrouter"
        return "gemini"
    return pref


def resolve_carousel_llm(
    provider: str | None = None,
    model: str | None = None,
) -> CarouselLlmKwargs:
    """Resolve one immutable per-request LLM configuration.

    Explicit values come from the carousel run request and never mutate runtime
    settings. Missing values retain the configured defaults.
    """
    settings = get_settings()
    runtime = get_runtime_settings()
    or_model = (runtime.openrouter_model or "").strip() or (
        settings.openrouter_model or "anthropic/claude-sonnet-4"
    ).strip()
    claude_key = (
        settings.anthropic_api_key or settings.claude_api_key or ""
    ).strip()
    resolved_provider = effective_carousel_provider(provider)
    requested_model = (model or "").strip()
    claude_model = resolve_claude_model()
    gemini_model = (settings.gemini_model or "").strip()
    if requested_model:
        if resolved_provider == "claude":
            claude_model = requested_model
        elif resolved_provider == "openrouter":
            or_model = requested_model
        elif resolved_provider == "gemini":
            gemini_model = requested_model

    return {
        "provider": resolved_provider,
        "api_key": (settings.gemini_api_key or "").strip(),
        "model": gemini_model,
        "claude_api_key": claude_key,
        "claude_model": claude_model,
        "openrouter_api_key": (settings.openrouter_api_key or "").strip(),
        "openrouter_model": or_model,
        "openrouter_base_url": (
            settings.openrouter_base_url or "https://openrouter.ai/api/v1"
        ).strip(),
    }


def carousel_llm_cache_id(pack: CarouselLlmKwargs | None = None) -> str:
    """Cache identity: provider + model for the *effective* route."""
    p = pack or resolve_carousel_llm()
    pref = normalize_carousel_llm_provider(p.get("provider"))
    claude_model = (p.get("claude_model") or "").strip() or DEFAULT_CLAUDE_MODEL
    or_model = (p.get("openrouter_model") or "").strip()
    gemini_model = (p.get("model") or "").strip() or "gemini"

    if pref == "claude":
        return f"claude:{claude_model}"[:128]
    if pref == "openrouter":
        return f"openrouter:{or_model or 'default'}"[:128]
    if pref == "gemini":
        return f"gemini:{gemini_model}"[:128]
    # auto fallback identity (should be rare after effective_carousel_provider)
    if (p.get("claude_api_key") or "").strip():
        return f"claude:{claude_model}"[:128]
    if (p.get("openrouter_api_key") or "").strip() and or_model:
        return f"openrouter:{or_model}"[:128]
    return f"gemini:{gemini_model}"[:128]


def openrouter_configured() -> bool:
    return bool((get_settings().openrouter_api_key or "").strip())


def carousel_llm_settings_public(
    model_options: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Safe fields for SettingsOut / picker endpoints (never expose the key)."""
    runtime = get_runtime_settings()
    settings = get_settings()
    or_model = (runtime.openrouter_model or "").strip() or (
        settings.openrouter_model or "anthropic/claude-sonnet-4"
    ).strip()
    stored = normalize_carousel_llm_provider(runtime.carousel_llm_provider)
    # UI default: show Claude when stored is auto and key exists.
    display = effective_carousel_provider(stored)
    gemini_model = (settings.gemini_model or "").strip() or "gemini-2.5-flash"
    options = list(model_options) if model_options is not None else list(CAROUSEL_LLM_MODEL_OPTIONS)
    return {
        "carousel_llm_provider": display if stored == "auto" else stored,
        "openrouter_model": or_model,
        "claude_model": resolve_claude_model(),
        "gemini_model": gemini_model,
        "claude_configured": claude_configured(),
        "openrouter_configured": openrouter_configured(),
        "gemini_configured": bool((settings.gemini_api_key or "").strip()),
        "carousel_llm_providers": list(CAROUSEL_LLM_PROVIDER_OPTIONS),
        "carousel_llm_model_options": options,
        # Alias for older /test clients that expect `models`.
        "models": options,
        "providers": list(CAROUSEL_LLM_PROVIDER_OPTIONS),
        "current": {
            "carousel_llm_provider": display if stored == "auto" else stored,
            "openrouter_model": or_model,
            "claude_model": resolve_claude_model(),
            "gemini_model": gemini_model,
        },
    }


async def carousel_llm_settings_public_live() -> dict[str, Any]:
    """Like ``carousel_llm_settings_public`` but model options come from provider APIs."""
    from app.llm.provider_models import fetch_live_carousel_model_options

    live = await fetch_live_carousel_model_options()
    if not live:
        return carousel_llm_settings_public()
    # If a provider fetch failed, keep curated fallbacks for that provider only.
    by_provider: dict[str, list[dict[str, str]]] = {"claude": [], "openrouter": [], "gemini": []}
    for row in live:
        prov = row.get("provider") or ""
        if prov in by_provider:
            by_provider[prov].append(row)
    merged: list[dict[str, str]] = []
    for prov, fallback in (
        ("claude", CLAUDE_DIRECT_MODEL_OPTIONS),
        ("openrouter", OPENROUTER_MODEL_OPTIONS),
        ("gemini", GEMINI_MODEL_OPTIONS),
    ):
        merged.extend(by_provider[prov] or list(fallback))
    return carousel_llm_settings_public(merged)
