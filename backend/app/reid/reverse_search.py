"""
Append-only web-identification layer for faces.

Free reverse-image primary path (inspired by
https://github.com/SOME-1HING/google-reverse-image-api ):
  public face thumbnail URL
    → Google reverse image search (hosted API, then direct scrape fallback)
    → resultText / similar images
    → optional Cohesivity Exa people search for LinkedIn profiles

Optional paid fallback: SERPAPI_KEY → Google Lens.
"""

from __future__ import annotations

import html as html_lib
import logging
import os
import re
from html.parser import HTMLParser
from urllib.parse import quote, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Face, FaceWebMatch, Person
from app.reid.cohesivity import cohesivity_application_key

logger = logging.getLogger(__name__)

_PROVIDER_GOOGLE = "google_reverse_image"
_PROVIDER_EXA = "cohesivity_exa_people"
_PROVIDER_SERPAPI = "serpapi_google_lens"
_LINKEDIN_RE = re.compile(r"linkedin\.com/(in|pub)/", re.IGNORECASE)
_MAX_STORED_RESULTS = 8
_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 6.0.1; SM-G920V Build/MMB29K) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/52.0.2743.98 Mobile Safari/537.36"
)
_COH_UA = "skill-3ec4ad99e463:cursor"


class ReverseSearchNotConfigured(RuntimeError):
    """Raised when no reverse-image or people-search provider can run."""


def provider_configured() -> bool:
    """Google reverse needs a public thumbnail URL; Exa/SerpAPI are optional boosters."""
    settings = get_settings()
    return bool(
        settings.public_base_url
        or settings.google_redirect_uri
        or cohesivity_application_key()
        or settings.serpapi_key
    )


def _public_thumbnail_url(face_id: int) -> str:
    settings = get_settings()
    base = settings.public_base_url.rstrip("/")
    if not base:
        parsed = urlparse(settings.google_redirect_uri)
        base = f"{parsed.scheme}://{parsed.netloc}"
    return f"{base}/faces/{face_id}/thumbnail"


# ── Google reverse (SOME-1HING style) ─────────────────────────────────────────


class _GoogleReverseParser(HTMLParser):
    """Pull the search-by-image guess text + input value from Google HTML."""

    def __init__(self) -> None:
        super().__init__()
        self._capture_div = False
        self._div_depth = 0
        self.result_text_parts: list[str] = []
        self.input_value: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = {k: (v or "") for k, v in attrs}
        classes = attrs_d.get("class", "").split()
        if tag == "input" and "gLFyf" in classes and attrs_d.get("value"):
            if self.input_value is None:
                self.input_value = attrs_d["value"]
        if tag == "div" and "r5a77d" in classes:
            self._capture_div = True
            self._div_depth = 1
            return
        if self._capture_div and tag == "div":
            self._div_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._capture_div and tag == "div":
            self._div_depth -= 1
            if self._div_depth <= 0:
                self._capture_div = False

    def handle_data(self, data: str) -> None:
        if self._capture_div:
            text = data.strip()
            if text:
                self.result_text_parts.append(text)


def _parse_google_html(page: str) -> dict:
    parser = _GoogleReverseParser()
    parser.feed(page)
    guess = " ".join(parser.result_text_parts).strip()
    if not guess and parser.input_value:
        guess = parser.input_value.strip()
    guess = html_lib.unescape(guess).replace("Â", " ").strip()
    # Strip common "Results for" / "Possible related search" prefixes.
    guess = re.sub(r"^(Results for|Possible related search[:\s]*)", "", guess, flags=re.I).strip()
    similar = ""
    if parser.input_value:
        similar = f"https://www.google.com/search?tbm=isch&q={quote(parser.input_value)}"
    elif guess:
        similar = f"https://www.google.com/search?tbm=isch&q={quote(guess)}"
    return {"resultText": guess, "similarUrl": similar}


async def _google_reverse_hosted(image_url: str) -> dict | None:
    """Call the public SOME-1HING Vercel API (free, no key)."""
    settings = get_settings()
    api = settings.google_reverse_api_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            response = await client.post(api, json={"imageUrl": image_url})
            if response.status_code >= 400:
                logger.warning("Google reverse hosted API HTTP %s", response.status_code)
                return None
            payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Google reverse hosted API failed: %s", exc)
        return None
    if not payload.get("success"):
        logger.info("Google reverse hosted: %s", payload.get("message"))
        return None
    data = payload.get("data") or {}
    text = (data.get("resultText") or "").replace("Â", " ").strip()
    text = re.sub(r"^(Results for|Possible related search[:\s]*)", "", text, flags=re.I).strip()
    return {"resultText": text, "similarUrl": data.get("similarUrl") or ""}


async def _google_reverse_scrape(image_url: str) -> dict | None:
    """Direct scrape of images.google.com/searchbyimage (same logic as the JS module)."""
    url = f"https://images.google.com/searchbyimage?safe=off&sbisrc=tg&image_url={quote(image_url, safe='')}"
    try:
        async with httpx.AsyncClient(
            timeout=45.0,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            parsed = _parse_google_html(response.text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Google reverse scrape failed: %s", exc)
        return None
    if not parsed.get("resultText") and not parsed.get("similarUrl"):
        return None
    return parsed


async def google_reverse_image(image_url: str) -> dict | None:
    """Free reverse image search: hosted API first, then direct scrape."""
    result = await _google_reverse_hosted(image_url)
    if result and (result.get("resultText") or result.get("similarUrl")):
        return result
    return await _google_reverse_scrape(image_url)


# ── Exa people enrichment ─────────────────────────────────────────────────────


async def _exa_people_search(query: str) -> list[dict]:
    key = cohesivity_application_key()
    if not key or not query:
        return []
    settings = get_settings()
    url = f"{settings.cohesivity_exa_base_url.rstrip('/')}/search"
    body = {
        "query": query,
        "type": "auto",
        "numResults": 10,
        "category": "people",
        "includeDomains": ["linkedin.com", "about.me", "crunchbase.com"],
    }
    try:
        async with httpx.AsyncClient(timeout=45.0, headers={"User-Agent": _COH_UA}) as client:
            response = await client.post(url, params={"key": key}, json=body)
            if response.status_code >= 400:
                body.pop("includeDomains", None)
                response = await client.post(url, params={"key": key}, json=body)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Exa people search failed: %s", exc)
        return []

    results: list[dict] = []
    seen: set[str] = set()
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        link = item.get("url") or item.get("id")
        if not link or link in seen:
            continue
        seen.add(link)
        results.append(
            {
                "title": item.get("title") or item.get("author") or "",
                "link": link,
                "score": item.get("score"),
            }
        )
    return results


def _extract_serpapi_results(payload: dict) -> list[dict]:
    raw: list[dict] = []
    for key in ("visual_matches", "image_results", "inline_images", "organic_results"):
        items = payload.get(key)
        if isinstance(items, list):
            raw.extend(i for i in items if isinstance(i, dict))
    results: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        link = item.get("link") or item.get("source")
        if not link or link in seen:
            continue
        seen.add(link)
        results.append(
            {
                "title": item.get("title") or item.get("source_name") or "",
                "link": link,
                "score": item.get("relevance_score"),
            }
        )
    return results


async def _serpapi_lens_search(image_url: str) -> list[dict]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google_lens",
                "url": image_url,
                "api_key": settings.serpapi_key,
            },
        )
        response.raise_for_status()
        return _extract_serpapi_results(response.json())


def _persist_matches(
    session: AsyncSession,
    *,
    face: Face,
    provider: str,
    results: list[dict],
) -> tuple[list[FaceWebMatch], str | None]:
    linkedin_url = next((r["link"] for r in results if _LINKEDIN_RE.search(r["link"])), None)
    stored: list[FaceWebMatch] = []
    if results:
        for result in results[:_MAX_STORED_RESULTS]:
            stored.append(
                FaceWebMatch(
                    face_id=face.id,
                    person_id=face.person_id,
                    provider=provider,
                    status="found",
                    result_title=(result.get("title") or "")[:2000] or None,
                    result_url=result["link"],
                    linkedin_url=result["link"] if _LINKEDIN_RE.search(result["link"]) else None,
                    score=result.get("score"),
                )
            )
    else:
        stored.append(
            FaceWebMatch(
                face_id=face.id,
                person_id=face.person_id,
                provider=provider,
                status="none",
            )
        )
    session.add_all(stored)
    return stored, linkedin_url


async def reverse_search_face(session: AsyncSession, face_id: int) -> dict:
    """
    1) Free Google reverse image on the public thumbnail.
    2) Feed guessed name/text into Cohesivity Exa for LinkedIn (if configured).
    3) SerpAPI Lens only if Google reverse fails and a key is set.
    """
    settings = get_settings()
    face = await session.get(Face, face_id)
    if face is None:
        raise ValueError(f"Face {face_id} not found")
    if not face.thumbnail_path or not os.path.exists(face.thumbnail_path):
        raise ValueError(f"Face {face_id} has no thumbnail to search with")

    known_name: str | None = None
    if face.person_id is not None:
        person = await session.get(Person, face.person_id)
        known_name = person.name if person else None

    image_url = _public_thumbnail_url(face_id)
    google = await google_reverse_image(image_url)
    results: list[dict] = []
    provider = _PROVIDER_GOOGLE
    google_guess = (google or {}).get("resultText") or ""
    similar_url = (google or {}).get("similarUrl") or ""

    if similar_url:
        results.append(
            {
                "title": google_guess or "Google similar images",
                "link": similar_url,
                "score": None,
            }
        )

    query_for_profiles = google_guess or known_name or ""
    if query_for_profiles and cohesivity_application_key():
        exa_hits = await _exa_people_search(f"{query_for_profiles} LinkedIn")
        if not exa_hits and google_guess:
            exa_hits = await _exa_people_search(google_guess)
        # Prefer LinkedIn hits; keep Google similar URL as context.
        linkedin_first = [h for h in exa_hits if _LINKEDIN_RE.search(h["link"])]
        other = [h for h in exa_hits if not _LINKEDIN_RE.search(h["link"])]
        results = linkedin_first + other + results
        if exa_hits:
            provider = f"{_PROVIDER_GOOGLE}+{_PROVIDER_EXA}"

    if not results and settings.serpapi_key:
        results = await _serpapi_lens_search(image_url)
        provider = _PROVIDER_SERPAPI

    if not google and not results and not settings.serpapi_key and not cohesivity_application_key():
        raise ReverseSearchNotConfigured(
            "Google reverse returned nothing and no Exa/SerpAPI fallback is configured. "
            "Ensure PUBLIC_BASE_URL points at a reachable backend so Google can fetch the thumbnail."
        )

    stored, linkedin_url = _persist_matches(session, face=face, provider=provider, results=results)
    await session.commit()

    return {
        "face_id": face_id,
        "provider": provider,
        "image_url": image_url,
        "google_guess": google_guess or None,
        "result_count": len([m for m in stored if m.status == "found"]),
        "linkedin_url": linkedin_url,
        "matches": [
            {
                "title": m.result_title,
                "url": m.result_url,
                "linkedin_url": m.linkedin_url,
                "score": m.score,
            }
            for m in stored
            if m.status == "found"
        ],
    }


async def web_matches_for_face(session: AsyncSession, face_id: int) -> list[FaceWebMatch]:
    return (
        (
            await session.execute(
                select(FaceWebMatch)
                .where(FaceWebMatch.face_id == face_id)
                .order_by(FaceWebMatch.id.desc())
            )
        )
        .scalars()
        .all()
    )


async def linkedin_map(session: AsyncSession) -> dict[str, str]:
    rows = (
        await session.execute(
            select(Person.name, FaceWebMatch.linkedin_url)
            .join(FaceWebMatch, FaceWebMatch.person_id == Person.id)
            .where(FaceWebMatch.linkedin_url.is_not(None))
            .order_by(FaceWebMatch.id.desc())
        )
    ).all()
    mapping: dict[str, str] = {}
    for name, url in rows:
        mapping.setdefault(name, url)
    return mapping
