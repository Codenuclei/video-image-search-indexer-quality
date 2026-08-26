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


# Arena / picker ids that Anthropic and Google do not accept on the direct
# Messages / generateContent APIs. OpenRouter does accept the slashed twins.
_DIRECT_TO_OPENROUTER: dict[str, str] = {
    "claude-fable-5": "anthropic/claude-fable-5",
    "claude-opus-4-6": "anthropic/claude-opus-4.6",
    "claude-opus-4-6-high": "anthropic/claude-opus-4.6",
    "claude-opus-4-7": "anthropic/claude-opus-4.7",
    "claude-opus-4-7-high": "anthropic/claude-opus-4.7",
    "claude-opus-5": "anthropic/claude-opus-5",
    "claude-opus-5-high": "anthropic/claude-opus-5",
    "gemini-3.7-flash": "google/gemini-3.7-flash",
    "gemini-3.7-flash-high": "google/gemini-3.7-flash",
    "gemini-3.1-pro-preview": "google/gemini-3.1-pro-preview",
    "gemini-3-pro-preview": "google/gemini-3-pro-preview",
    "gemini-3-pro": "google/gemini-3-pro-preview",
}


def prefer_openrouter_first(provider: str, model_id: str) -> bool:
    """True when the Google/Anthropic call is likely to hang past Railway's edge.

    Arena / preview Gemini ids often take longer than the public proxy budget
    (~60–100s) even when they eventually 200. OpenRouter twins finish in time.
    """
    pref = normalize_carousel_llm_provider(provider)
    mid = (model_id or "").strip().lower()
    if pref == "gemini":
        return True
    if mid in _DIRECT_TO_OPENROUTER:
        return True
    if "preview" in mid or "fable" in mid:
        return True
    return False


def openrouter_slug_for_direct(provider: str, model_id: str) -> str:
    """Map a Claude/Gemini picker id onto an OpenRouter model slug."""
    mid = (model_id or "").strip()
    if not mid:
        return ""
    if "/" in mid:
        return mid
    mapped = _DIRECT_TO_OPENROUTER.get(mid) or _DIRECT_TO_OPENROUTER.get(mid.lower())
    if mapped:
        return mapped
    pref = normalize_carousel_llm_provider(provider)
    dotted = mid.replace("-4-6", "-4.6").replace("-4-7", "-4.7")
    if pref == "claude" or dotted.startswith("claude"):
        return f"anthropic/{dotted}"
    if pref == "gemini" or dotted.startswith("gemini"):
        return f"google/{dotted}"
    return mid


def vision_hops(pack: CarouselLlmKwargs | dict[str, Any]) -> list[tuple[str, str, str]]:
    """Ready vision hops as ``(provider, model_id, api_key)`` honoring the studio picker.

    Same hop order as ``_llm_complete_json`` so Claude/Gemini on OpenRouter
    stay on OpenRouter instead of falling through to a hardcoded Gemini id.
    """
    pref = normalize_carousel_llm_provider(pack.get("provider"))
    or_key = (pack.get("openrouter_api_key") or "").strip()
    or_model = (pack.get("openrouter_model") or "").strip()
    claude_key = (pack.get("claude_api_key") or "").strip()
    claude_model = (pack.get("claude_model") or "").strip()
    gemini_key = (pack.get("api_key") or "").strip()
    gemini_model = (pack.get("model") or "").strip()
    if pref == "claude":
        or_model = openrouter_slug_for_direct("claude", claude_model) or or_model
    elif pref == "gemini":
        or_model = openrouter_slug_for_direct("gemini", gemini_model) or or_model

    names: list[str] = []
    if pref == "auto":
        if claude_key:
            names.append("claude")
        if or_key and or_model:
            names.append("openrouter")
        if gemini_key and gemini_model:
            names.append("gemini")
    elif pref == "openrouter":
        names.append("openrouter")
        if claude_key:
            names.append("claude")
        if gemini_key and gemini_model:
            names.append("gemini")
    elif pref == "claude":
        if or_key and or_model and prefer_openrouter_first("claude", claude_model):
            names.extend(["openrouter", "claude"])
        else:
            names.append("claude")
            if or_key and or_model:
                names.append("openrouter")
    elif pref == "gemini":
        if or_key and or_model and prefer_openrouter_first("gemini", gemini_model):
            names.extend(["openrouter", "gemini"])
        else:
            names.append("gemini")
            if or_key and or_model:
                names.append("openrouter")

    hops: list[tuple[str, str, str]] = []
    for name in names:
        if name == "openrouter" and or_key and or_model:
            hops.append(("openrouter", or_model, or_key))
        elif name == "claude" and claude_key:
            hops.append(("claude", claude_model or DEFAULT_CLAUDE_MODEL, claude_key))
        elif name == "gemini" and gemini_key and gemini_model:
            hops.append(("gemini", gemini_model, gemini_key))
    return hops


def vision_ready(pack: CarouselLlmKwargs | dict[str, Any] | None, *, api_key: str = "") -> bool:
    if pack and vision_hops(pack):
        return True
    return bool((api_key or "").strip())


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
