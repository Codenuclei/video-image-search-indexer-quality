"""Carousel Studio owns GIS + Drive OAuth in this process (not a search sidecar)."""

from unittest.mock import patch

from fastapi import HTTPException

from app.auth_credentials import carousel_google
from app.config import Settings
from app.routers import carousel_auth, carousel_oauth
from app.routers.carousel_auth import _verify_google_id_token


def test_carousel_google_prefers_carousel_env() -> None:
    settings = Settings(
        carousel_google_client_id="carousel-id",
        carousel_google_client_secret="carousel-secret",
        carousel_google_redirect_uri="https://carousel.example/auth/google/callback",
        carousel_google_api_key="carousel-key",
        google_client_id="search-id",
        google_client_secret="search-secret",
        google_redirect_uri="https://search.example/auth/google/callback",
        google_api_key="search-key",
    )
    creds = carousel_google(settings)
    assert creds.client_id == "carousel-id"
    assert creds.client_secret == "carousel-secret"
    assert creds.redirect_uri == "https://carousel.example/auth/google/callback"
    assert creds.api_key == "carousel-key"


def test_carousel_google_falls_back_to_google_env() -> None:
    settings = Settings(
        carousel_google_client_id="",
        carousel_google_client_secret="",
        carousel_google_redirect_uri="",
        carousel_google_api_key="",
        google_client_id="legacy-id",
        google_client_secret="legacy-secret",
        google_redirect_uri="http://localhost:8000/auth/google/callback",
        google_api_key="legacy-key",
    )
    creds = carousel_google(settings)
    assert creds.client_id == "legacy-id"
    assert creds.client_secret == "legacy-secret"
    assert creds.redirect_uri == "http://localhost:8000/auth/google/callback"
    assert creds.api_key == "legacy-key"


def test_gis_verify_uses_this_service_client_id() -> None:
    settings = Settings(
        carousel_google_client_id="carousel-gis",
        google_client_id="search-gis",
    )
    with patch("app.routers.carousel_auth.google_id_token.verify_oauth2_token") as mock:
        mock.return_value = {"email": "a@mastersunion.org", "hd": "mastersunion.org"}
        payload = _verify_google_id_token("x" * 24, settings)
    assert payload["email"] == "a@mastersunion.org"
    assert mock.call_args.args[2] == "carousel-gis"


def test_gis_verify_requires_client_id() -> None:
    settings = Settings(google_client_id="", carousel_google_client_id="")
    try:
        _verify_google_id_token("x" * 24, settings)
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "CAROUSEL_GOOGLE_CLIENT_ID" in str(exc.detail)
    else:
        raise AssertionError("expected 503 when client id is missing")


def test_own_auth_routes_are_mounted() -> None:
    gis_paths = {route.path for route in carousel_auth.router.routes}
    oauth_paths = {route.path for route in carousel_oauth.router.routes}
    assert "/auth/google-id-token" in gis_paths
    assert "/auth/is-admin" in gis_paths
    assert "/auth/google" in oauth_paths
    assert "/auth/google/callback" in oauth_paths
    assert "/api/session" in oauth_paths
    assert "/api/drive-token" in oauth_paths

    from app.main import app

    app_paths = set(app.openapi().get("paths", {}))
    assert "/auth/google-id-token" in app_paths
    assert "/auth/google" in app_paths
    assert "/api/session" in app_paths
