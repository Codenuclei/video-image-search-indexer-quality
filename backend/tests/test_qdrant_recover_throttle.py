"""Qdrant status recovery must not run full-collection scans on every tick."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.workers import maintenance


class _StubSession:
    async def __aenter__(self) -> "_StubSession":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


@pytest.fixture
def recover_env(monkeypatch):
    """Reset module state and stub the expensive recovery + counts calls."""
    calls = {"recover": 0, "counts": 0}
    counts_value = {"images": 10, "frames": 100, "captions": 5}

    def fake_counts() -> dict[str, int]:
        calls["counts"] += 1
        return dict(counts_value)

    async def fake_recover(session, *, dry_run=True, **_kwargs):
        calls["recover"] += 1
        return SimpleNamespace(status_marked_processed=0)

    monkeypatch.setattr(
        "app.qdrant.recover.collection_point_counts", fake_counts
    )
    monkeypatch.setattr("app.qdrant.recover.recover_from_qdrant", fake_recover)
    monkeypatch.setattr(maintenance, "get_session_factory", lambda: _StubSession)
    monkeypatch.setattr(maintenance, "_recover_running", False)
    monkeypatch.setattr(maintenance, "_last_recover_at", None)
    monkeypatch.setattr(maintenance, "_last_recover_counts", None)
    return calls, counts_value


@pytest.mark.asyncio
async def test_recovery_runs_once_then_is_throttled(recover_env) -> None:
    calls, _counts = recover_env

    await maintenance._recover_status_from_qdrant()
    assert calls["recover"] == 1

    # Immediately after, the interval throttle blocks another full scan.
    await maintenance._recover_status_from_qdrant()
    await maintenance._recover_status_from_qdrant()
    assert calls["recover"] == 1
    assert calls["counts"] == 1


@pytest.mark.asyncio
async def test_recovery_skips_scan_when_counts_unchanged(recover_env, monkeypatch) -> None:
    calls, _counts = recover_env

    await maintenance._recover_status_from_qdrant()
    assert calls["recover"] == 1

    # Interval elapsed but nothing changed in Qdrant → counts gate skips scan.
    monkeypatch.setattr(
        maintenance,
        "_last_recover_at",
        datetime.now(tz=timezone.utc) - timedelta(hours=12),
    )
    await maintenance._recover_status_from_qdrant()
    assert calls["counts"] == 2
    assert calls["recover"] == 1


@pytest.mark.asyncio
async def test_recovery_runs_again_when_counts_changed(recover_env, monkeypatch) -> None:
    calls, counts_value = recover_env

    await maintenance._recover_status_from_qdrant()
    assert calls["recover"] == 1

    counts_value["frames"] = 999
    monkeypatch.setattr(
        maintenance,
        "_last_recover_at",
        datetime.now(tz=timezone.utc) - timedelta(hours=12),
    )
    await maintenance._recover_status_from_qdrant()
    assert calls["recover"] == 2


@pytest.mark.asyncio
async def test_force_bypasses_throttle_but_not_overlap_guard(recover_env, monkeypatch) -> None:
    calls, _counts = recover_env

    await maintenance._recover_status_from_qdrant()
    await maintenance._recover_status_from_qdrant(force=True)
    assert calls["recover"] == 2

    monkeypatch.setattr(maintenance, "_recover_running", True)
    await maintenance._recover_status_from_qdrant(force=True)
    assert calls["recover"] == 2
