"""Google ID-token audiences: primary + extra GIS clients."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.routers import app_auth


def _settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_audiences_include_primary_and_extras() -> None:
    settings = _settings(
        google_client_id="primary.apps.googleusercontent.com",
        google_id_token_extra_audiences=(
            "extra-a.apps.googleusercontent.com, extra-b.apps.googleusercontent.com, "
            "primary.apps.googleusercontent.com"
        ),
    )
    assert app_auth._google_id_token_audiences(settings) == [
        "primary.apps.googleusercontent.com",
        "extra-a.apps.googleusercontent.com",
        "extra-b.apps.googleusercontent.com",
    ]


def test_audiences_default_includes_digitalocean_gis_client() -> None:
    settings = _settings(google_client_id="primary.apps.googleusercontent.com")
    auds = app_auth._google_id_token_audiences(settings)
    assert auds[0] == "primary.apps.googleusercontent.com"
    assert (
        "667640945733-1m2i2ipuib6mu51a7o9d76imiatl6ka0.apps.googleusercontent.com"
        in auds
    )


def test_audiences_default_includes_searchatmu_domain_client() -> None:
    settings = _settings(google_client_id="primary.apps.googleusercontent.com")
    auds = app_auth._google_id_token_audiences(settings)
    assert (
        "969672000852-9aauao6fkmtb7ga7o1m1qvsec3145dfk.apps.googleusercontent.com"
        in auds
    )


def test_verify_accepts_token_for_extra_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        google_client_id="primary.apps.googleusercontent.com",
        google_id_token_extra_audiences="extra.apps.googleusercontent.com",
    )
    seen: dict[str, Any] = {}

    def _fake_verify(credential: str, request: Any, audience: Any) -> dict[str, Any]:
        seen["credential"] = credential
        seen["audience"] = audience
        return {
            "email": "user@mastersunion.org",
            "hd": "mastersunion.org",
            "aud": "extra.apps.googleusercontent.com",
        }

    monkeypatch.setattr(app_auth.google_id_token, "verify_oauth2_token", _fake_verify)
    monkeypatch.setattr(app_auth.google_requests, "Request", MagicMock)

    payload = app_auth._verify_google_id_token("fake-credential-token-value", settings)
    assert payload["email"] == "user@mastersunion.org"
    assert seen["audience"] == [
        "primary.apps.googleusercontent.com",
        "extra.apps.googleusercontent.com",
    ]


def test_verify_rejects_unknown_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        google_client_id="primary.apps.googleusercontent.com",
        google_id_token_extra_audiences="extra.apps.googleusercontent.com",
    )

    def _fake_verify(_credential: str, _request: Any, _audience: Any) -> dict[str, Any]:
        raise ValueError(
            "Token has wrong audience unknown.apps.googleusercontent.com, "
            "expected one of ['primary.apps.googleusercontent.com']"
        )

    monkeypatch.setattr(app_auth.google_id_token, "verify_oauth2_token", _fake_verify)
    monkeypatch.setattr(app_auth.google_requests, "Request", MagicMock)

    with pytest.raises(HTTPException) as exc_info:
        app_auth._verify_google_id_token("fake-credential-token-value", settings)
    assert exc_info.value.status_code == 401
    assert "Invalid Google sign-in token" in str(exc_info.value.detail)


def test_verify_503_when_no_audience_configured() -> None:
    settings = _settings(google_client_id="", google_id_token_extra_audiences="")
    with pytest.raises(HTTPException) as exc_info:
        app_auth._verify_google_id_token("fake-credential-token-value", settings)
    assert exc_info.value.status_code == 503
