"""Safety and behavior tests for the protected production diagnostics API."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException

from app.config import Settings
from app.routers.diagnostics import (
    _authorize,
    _run_fixed_suite,
    _safe_subprocess_env,
)
from app.routers import diagnostics


def test_diagnostics_disabled_is_hidden() -> None:
    with pytest.raises(HTTPException) as exc:
        _authorize(
            Settings(production_tests_enabled=False, production_tests_token="secret"),
            "secret",
        )
    assert exc.value.status_code == 404


def test_diagnostics_requires_constant_secret() -> None:
    settings = Settings(production_tests_enabled=True, production_tests_token="secret")
    with pytest.raises(HTTPException) as exc:
        _authorize(settings, "wrong")
    assert exc.value.status_code == 401
    _authorize(settings, "secret")


def test_runner_environment_blocks_live_credentials(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://production")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://production_test")
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-leak")
    env = _safe_subprocess_env()
    assert "production" not in env["DATABASE_URL"]
    assert "production" not in env["TEST_DATABASE_URL"]
    assert "GEMINI_API_KEY" not in env


@pytest.mark.asyncio
async def test_fixed_suite_uses_only_hard_coded_module() -> None:
    process = AsyncMock()
    process.communicate.return_value = (
        json.dumps({"ok": True, "suite": "production-safe-v1", "checks": []}).encode(),
        b"",
    )
    process.returncode = 0
    with patch(
        "app.routers.diagnostics.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ) as spawn:
        result = await _run_fixed_suite(
            Settings(
                production_tests_enabled=True,
                production_tests_token="secret",
                production_tests_timeout_seconds=5,
            )
        )
    assert result["ok"] is True
    args = spawn.await_args.args
    assert args[1:] == ("-m", "app.diagnostics.production_suite")


@pytest.mark.asyncio
async def test_post_tests_is_hidden_then_secret_gated() -> None:
    from httpx import ASGITransport, AsyncClient

    from app.db.session import get_db

    app = FastAPI()
    app.include_router(diagnostics.router)

    async def fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = fake_db
    transport = ASGITransport(app=app)
    diagnostics._last_run_mono = 0.0

    with (
        patch(
            "app.routers.diagnostics._run_fixed_suite",
            new=AsyncMock(return_value={"ok": True, "suite": "production-safe-v1"}),
        ),
        patch(
            "app.routers.diagnostics._live_read_only_checks",
            new=AsyncMock(return_value={"ok": True}),
        ),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            app.dependency_overrides[diagnostics.get_settings] = lambda: Settings(
                production_tests_enabled=False,
                production_tests_token="secret",
            )
            hidden = await client.post("/tests", headers={"X-Tests-Token": "secret"})
            assert hidden.status_code == 404

            app.dependency_overrides[diagnostics.get_settings] = lambda: Settings(
                production_tests_enabled=True,
                production_tests_token="secret",
                production_tests_cooldown_seconds=0,
            )
            denied = await client.post("/tests", headers={"X-Tests-Token": "wrong"})
            assert denied.status_code == 401
            ok = await client.post("/tests", headers={"X-Tests-Token": "secret"})
            assert ok.status_code == 200
            assert ok.json()["ok"] is True
