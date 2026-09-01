"""OAuth return_to allowlist is carousel-only on this fork (no search frontend)."""

from app.config import Settings
from app.routers.carousel_oauth import resolve_oauth_return_url


def test_oauth_return_defaults_to_carousel() -> None:
    settings = Settings(
        frontend_url="https://dfi-frontend.example",
        carousel_frontend_url="https://dfi-carousel.example",
        allowed_origins="",
    )
    assert resolve_oauth_return_url(settings, None) == "https://dfi-carousel.example/carousel"
    assert resolve_oauth_return_url(settings, "") == "https://dfi-carousel.example/carousel"


def test_oauth_return_relative_carousel_path() -> None:
    settings = Settings(
        frontend_url="https://dfi-frontend.example",
        carousel_frontend_url="https://dfi-carousel.example",
        allowed_origins="",
    )
    assert (
        resolve_oauth_return_url(settings, "/carousel")
        == "https://dfi-carousel.example/carousel"
    )
    assert (
        resolve_oauth_return_url(settings, "/test/studio")
        == "https://dfi-carousel.example/test/studio"
    )


def test_oauth_return_absolute_allowlisted() -> None:
    settings = Settings(
        frontend_url="https://dfi-frontend.example",
        carousel_frontend_url="https://dfi-carousel.example",
        allowed_origins="https://extra.example",
    )
    assert (
        resolve_oauth_return_url(settings, "https://dfi-carousel.example/carousel")
        == "https://dfi-carousel.example/carousel"
    )
    assert (
        resolve_oauth_return_url(settings, "https://extra.example/test/studio")
        == "https://extra.example/test/studio"
    )


def test_oauth_return_rejects_search_frontend_origin() -> None:
    settings = Settings(
        frontend_url="https://dfi-frontend.example",
        carousel_frontend_url="https://dfi-carousel.example",
        allowed_origins="",
    )
    assert (
        resolve_oauth_return_url(settings, "https://dfi-frontend.example/folders")
        == "https://dfi-carousel.example/carousel"
    )
    assert (
        resolve_oauth_return_url(settings, "https://evil.example/phish")
        == "https://dfi-carousel.example/carousel"
    )
