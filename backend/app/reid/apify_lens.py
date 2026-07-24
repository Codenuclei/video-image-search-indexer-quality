"""
Apify Google Lens reverse-image (identity-grade).

Uses actor borderline/google-lens — AI Mode + exact/visual matches — which
matches browser Google Lens far better than Cloud Vision WEB_DETECTION.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_PROVIDER = "apify_google_lens"
_DEFAULT_ACTOR = "borderline/google-lens"


class ApifyLensNotConfigured(RuntimeError):
    """Raised when APIFY_TOKEN is missing."""


class ApifyLensError(RuntimeError):
    """Raised when the Apify Google Lens actor fails."""


def apify_configured() -> bool:
    return bool(get_settings().apify_token)


def _actor_id(settings) -> str:
    # Apify API uses ~ between user and name
    raw = (settings.apify_google_lens_actor or _DEFAULT_ACTOR).strip()
    return raw.replace("/", "~")


def _extract_matches(items: list[dict]) -> tuple[str | None, list[dict]]:
    """
    Normalize Apify Lens dataset rows into (ai_guess, [{title, link, score, thumbnail}]).
    Actor payload shapes vary by searchType; be defensive.
    """
    guess: str | None = None
    results: list[dict] = []
    seen: set[str] = set()

    def add(title: str | None, link: str | None, *, score=None, thumbnail: str | None = None) -> None:
        if not link or link in seen:
            return
        seen.add(link)
        results.append(
            {
                "title": (title or "").strip() or link,
                "link": link,
                "score": score,
                "thumbnail": thumbnail,
            }
        )

    for item in items:
        if not isinstance(item, dict):
            continue

        # AI mode text / overview
        for key in ("aiAnswer", "aiOverview", "aiDescription", "answer", "description", "summary"):
            val = item.get(key)
            if isinstance(val, str) and val.strip() and not guess:
                guess = val.strip()[:500]

        # Nested AI block
        ai = item.get("aiMode") or item.get("ai") or {}
        if isinstance(ai, dict):
            for key in ("answer", "text", "description", "overview"):
                val = ai.get(key)
                if isinstance(val, str) and val.strip() and not guess:
                    guess = val.strip()[:500]
            for hit in ai.get("matches") or ai.get("results") or []:
                if isinstance(hit, dict):
                    add(hit.get("title") or hit.get("source"), hit.get("link") or hit.get("url"), thumbnail=hit.get("thumbnail"))

        # Exact / visual / general match lists
        for list_key in (
            "exactMatches",
            "exact_match",
            "visualMatches",
            "visual_match",
            "matches",
            "results",
            "organicResults",
            "knowledgeGraph",
        ):
            block = item.get(list_key)
            if isinstance(block, dict):
                block = block.get("matches") or block.get("results") or [block]
            if not isinstance(block, list):
                continue
            for hit in block:
                if not isinstance(hit, dict):
                    continue
                add(
                    hit.get("title") or hit.get("source") or hit.get("name"),
                    hit.get("link") or hit.get("url") or hit.get("pageUrl"),
                    score=hit.get("score") or hit.get("rank"),
                    thumbnail=hit.get("thumbnail") or hit.get("imageUrl") or hit.get("image"),
                )

        # Flat single result
        add(item.get("title") or item.get("source"), item.get("link") or item.get("url"), thumbnail=item.get("thumbnail"))

    return guess, results


async def apify_google_lens_search(
    *,
    image_url: str | None = None,
    image_bytes: bytes | None = None,
    max_wait_secs: int = 120,
) -> dict:
    """
    Run borderline/google-lens and return:
      { provider, google_guess, matches:[{title,link,score,thumbnail}], raw_count }
    Prefer image_bytes (base64) so Google does not need to fetch our thumbnail URL.
    """
    settings = get_settings()
    token = settings.apify_token
    if not token:
        raise ApifyLensNotConfigured("Set APIFY_TOKEN to use Apify Google Lens reverse image search.")

    if not image_url and not image_bytes:
        raise ValueError("Provide image_url or image_bytes")

    payload: dict = {
        "searchTypes": ["ai-mode", "exact-match", "visual-match"],
        "language": "en",
    }
    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload["imagesBase64"] = [f"data:image/jpeg;base64,{b64}"]
    elif image_url:
        payload["imageUrls"] = [{"url": image_url}]

    actor = _actor_id(settings)
    sync_url = (
        f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
        f"?token={token}&timeout={max_wait_secs}"
    )

    async with httpx.AsyncClient(timeout=float(max_wait_secs) + 30.0) as client:
        response = await client.post(sync_url, json=payload)
        if response.status_code >= 400:
            detail = response.text[:1500]
            raise ApifyLensError(f"Apify Lens HTTP {response.status_code}: {detail}")
        try:
            items = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ApifyLensError(f"Apify Lens returned non-JSON: {exc}") from exc

    if not isinstance(items, list):
        items = [items] if isinstance(items, dict) else []

    guess, matches = _extract_matches(items)
    logger.info(
        "Apify Lens: guess=%s matches=%s items=%s",
        (guess or "")[:80],
        len(matches),
        len(items),
    )
    return {
        "provider": _PROVIDER,
        "google_guess": guess,
        "matches": matches,
        "raw_count": len(items),
    }


async def apify_lens_for_face_thumbnail(thumbnail_path: str, *, image_url: str | None = None) -> dict:
    path = Path(thumbnail_path)
    if not path.is_file():
        raise ValueError(f"Thumbnail missing: {thumbnail_path}")
    return await apify_google_lens_search(image_bytes=path.read_bytes(), image_url=image_url)
