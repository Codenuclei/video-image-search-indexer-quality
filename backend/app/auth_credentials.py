"""This process's Google OAuth credentials (Carousel Studio is the auth server).

Prefer ``CAROUSEL_GOOGLE_*`` so a leftover search env file cannot silently reuse
search's client / callback. Empty carousel-specific vars fall back to ``GOOGLE_*``
in the same process — never HTTP to another backend.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class CarouselGoogleCreds:
    client_id: str
    client_secret: str
    redirect_uri: str
    api_key: str


def carousel_google(settings: Settings) -> CarouselGoogleCreds:
    client_id = (
        settings.carousel_google_client_id or settings.google_client_id or ""
    ).strip()
    client_secret = (
        settings.carousel_google_client_secret or settings.google_client_secret or ""
    ).strip()
    redirect_uri = (
        settings.carousel_google_redirect_uri or settings.google_redirect_uri or ""
    ).strip() or "http://localhost:8000/auth/google/callback"
    api_key = (settings.carousel_google_api_key or settings.google_api_key or "").strip()
    return CarouselGoogleCreds(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        api_key=api_key,
    )
