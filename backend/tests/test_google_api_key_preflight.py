"""Unit tests for Google Browser API key preflight (Picker origin / referrer)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from app.routers.drive_oauth import (
    PICKER_API_KEY_ORIGIN_DETAIL,
    _client_origin_from_request,
    _is_invalid_browser_api_key_response,
    preflight_google_browser_api_key,
)


def _mock_async_client(response: MagicMock | None = None, *, get_error: Exception | None = None):
    """Patch httpx.AsyncClient used by preflight_google_browser_api_key."""

    class _Client:
        last_get: dict | None = None

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params=None, headers=None):
            _Client.last_get = {"url": url, "params": params, "headers": headers}
            if get_error is not None:
                raise get_error
            assert response is not None
            return response

    return _Client


def test_client_origin_prefers_origin_header() -> None:
    request = MagicMock()
    request.headers = {"origin": "https://app.example.com", "referer": "https://other.example/path"}
    assert _client_origin_from_request(request) == "https://app.example.com"


def test_client_origin_falls_back_to_referer() -> None:
    request = MagicMock()
    request.headers = {"referer": "https://dfi.sslip.io/folders?x=1"}
    assert _client_origin_from_request(request) == "https://dfi.sslip.io"


def test_is_invalid_detects_api_key_invalid_in_details() -> None:
    payload = {
        "error": {
            "code": 400,
            "message": "API key not valid. Please pass a valid API key.",
            "status": "INVALID_ARGUMENT",
            "details": [{"reason": "API_KEY_INVALID"}],
        }
    }
    assert _is_invalid_browser_api_key_response(400, payload, str(payload)) is True


def test_is_invalid_detects_referrer_blocked() -> None:
    payload = {
        "error": {
            "code": 403,
            "message": "Requests from referer https://x.example/ are blocked.",
            "details": [{"reason": "API_KEY_HTTP_REFERRER_BLOCKED"}],
        }
    }
    assert _is_invalid_browser_api_key_response(403, payload, str(payload)) is True


def test_missing_oauth_is_not_invalid_key() -> None:
    payload = {
        "error": {
            "code": 401,
            "message": "Login Required.",
            "errors": [{"reason": "required", "domain": "global"}],
            "status": "UNAUTHENTICATED",
        }
    }
    assert _is_invalid_browser_api_key_response(401, payload, str(payload)) is False


@pytest.mark.asyncio
async def test_preflight_raises_on_invalid_key() -> None:
    payload = {
        "error": {
            "code": 400,
            "message": "API key not valid. Please pass a valid API key.",
            "details": [{"reason": "API_KEY_INVALID"}],
        }
    }
    resp = MagicMock()
    resp.status_code = 400
    resp.json.return_value = payload
    resp.text = str(payload)
    client_cls = _mock_async_client(resp)

    with patch("app.routers.drive_oauth.httpx.AsyncClient", client_cls):
        with pytest.raises(HTTPException) as exc_info:
            await preflight_google_browser_api_key("bad-key", "https://app.example.com")

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == PICKER_API_KEY_ORIGIN_DETAIL
    assert client_cls.last_get is not None
    assert client_cls.last_get["params"]["key"] == "bad-key"
    assert client_cls.last_get["headers"]["Referer"] == "https://app.example.com/"


@pytest.mark.asyncio
async def test_preflight_ok_when_missing_oauth_only() -> None:
    payload = {
        "error": {
            "code": 401,
            "message": "Login Required.",
            "errors": [{"reason": "required"}],
            "status": "UNAUTHENTICATED",
        }
    }
    resp = MagicMock()
    resp.status_code = 401
    resp.json.return_value = payload
    resp.text = str(payload)
    client_cls = _mock_async_client(resp)

    with patch("app.routers.drive_oauth.httpx.AsyncClient", client_cls):
        await preflight_google_browser_api_key("ok-key", "https://app.example.com")


@pytest.mark.asyncio
async def test_preflight_ok_on_network_error() -> None:
    client_cls = _mock_async_client(get_error=httpx.ConnectError("dns failed"))
    with patch("app.routers.drive_oauth.httpx.AsyncClient", client_cls):
        await preflight_google_browser_api_key("any-key", "https://app.example.com")
