"""Unit tests for Drive download/list concurrency (no Postgres / Drive required)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.drive.rate_limit import drive_download_semaphore, drive_list_semaphore
from app.pipelines.common import download_to_memory


@pytest.mark.asyncio
async def test_download_to_memory_acquires_drive_slot():
    entered = {"n": 0}

    class _Slot:
        async def __aenter__(self):
            entered["n"] += 1
            return None

        async def __aexit__(self, *exc):
            return False

    class _Resp:
        async def aiter_bytes(self, chunk_size=0):
            yield b"abc"

    class _Stream:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *exc):
            return False

    client = MagicMock()
    client.stream_file_content = MagicMock(return_value=_Stream())

    with patch("app.drive.rate_limit.drive_download_slot", _Slot):
        data = await download_to_memory(client, "file-1")

    assert data == b"abc"
    assert entered["n"] == 1


@pytest.mark.asyncio
async def test_download_semaphore_caps_concurrency():
    # Force a tiny semaphore
    with patch("app.drive.rate_limit.get_settings") as gs:
        gs.return_value = MagicMock(drive_download_max_concurrent=2, drive_list_max_concurrent=4)
        # Reset cached semaphore
        import app.drive.rate_limit as rl

        rl._download_sem = None
        rl._download_sem_n = None
        sem = drive_download_semaphore()

        active = 0
        peak = 0

        async def worker():
            nonlocal active, peak
            async with sem:
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.05)
                active -= 1

        await asyncio.gather(*[worker() for _ in range(8)])
        assert peak <= 2


@pytest.mark.asyncio
async def test_list_semaphore_caps_concurrency():
    with patch("app.drive.rate_limit.get_settings") as gs:
        gs.return_value = MagicMock(drive_download_max_concurrent=16, drive_list_max_concurrent=3)
        import app.drive.rate_limit as rl

        rl._list_sem = None
        rl._list_sem_n = None
        sem = drive_list_semaphore()

        active = 0
        peak = 0

        async def worker():
            nonlocal active, peak
            async with sem:
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.05)
                active -= 1

        await asyncio.gather(*[worker() for _ in range(9)])
        assert peak <= 3
