"""Fixed DB-free assertions invoked by the secret-gated production test API.

This is intentionally not a generic test runner: it accepts no arguments,
loads no pytest fixtures, and never opens a database connection.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def _row(
    file_id: str,
    name: str,
    path: str,
    status: str,
    *,
    content_hash: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=file_id,
        name=name,
        path=path,
        mime_type="image/jpeg",
        status=SimpleNamespace(value=status),
        size=100,
        source="drive",
        error_message=None,
        last_synced_at=None,
        content_hash=content_hash,
        content_hash_algo="md5" if content_hash else None,
    )


def check_library_shell() -> None:
    from app.drive.library_tree import (
        build_library_shell,
        compute_library_revision,
        folder_node_to_shell_dict,
        is_direct_child_of_folder,
    )

    rows = [
        _row("a", "a.jpg", "/Album/a.jpg", "processed"),
        _row("b", "b.jpg", "/Album/b.jpg", "pending"),
    ]
    root, summary = build_library_shell(rows)
    shell = folder_node_to_shell_dict(root)
    assert shell["files"] == []
    assert shell["folders"][0]["files"] == []
    assert summary["total_files"] == 2
    assert summary["caption_stats_ready"] is False
    assert compute_library_revision(rows).startswith("2:")
    assert is_direct_child_of_folder("/Album/a.jpg", "/Album")
    assert not is_direct_child_of_folder("/Album/Sub/a.jpg", "/Album")


def check_shell_cache() -> None:
    from app.drive.library_shell_cache import LibraryShellCache

    cache = LibraryShellCache()
    assert cache.get("r1") is None
    cache.put("r1", {"revision": "r1"})
    assert cache.get("r1") == {"revision": "r1"}
    assert cache.get("r2") is None
    cache.invalidate()
    assert cache.get("r1") is None


def check_content_hash_helpers() -> None:
    from app.drive.content_hash import is_macos_junk_name, sha256_bytes

    assert sha256_bytes(b"same bytes") == sha256_bytes(b"same bytes")
    assert sha256_bytes(b"same bytes") != sha256_bytes(b"different")
    assert is_macos_junk_name("._IMG_1.HEIC")
    assert not is_macos_junk_name("IMG_1.HEIC")


async def _check_pre_download_duplicate_gate() -> None:
    from app.config import Settings
    from app.db.models import DriveFile, DriveFileStatus
    from app.pipelines.image import prepare_image_media

    incoming = DriveFile(
        id="production-smoke-twin",
        name="copy.jpg",
        mime_type="image/jpeg",
        path="/copy.jpg",
        status=DriveFileStatus.PENDING,
        content_hash="knownhash",
        content_hash_algo="md5",
    )
    session = AsyncMock()
    client = AsyncMock()
    with (
        patch("app.pipelines.image.file_has_media", new=AsyncMock(return_value=False)),
        patch("app.pipelines.image.clear_existing_media", new=AsyncMock()),
        patch(
            "app.drive.conflicts.apply_dedupe_on_upsert",
            new=AsyncMock(return_value="duplicate_content"),
        ),
        patch("app.drive.media_cache.ensure_media_cached", new=AsyncMock()) as download,
        patch("app.pipelines.image.decode_image_bgr") as decode,
    ):
        result = await prepare_image_media(session, incoming, client, Settings())
    assert result is None
    download.assert_not_awaited()
    decode.assert_not_called()


def check_pre_download_duplicate_gate() -> None:
    asyncio.run(_check_pre_download_duplicate_gate())


CHECKS = (
    ("library_shell", check_library_shell),
    ("shell_cache", check_shell_cache),
    ("content_hash_helpers", check_content_hash_helpers),
    ("pre_download_duplicate_gate", check_pre_download_duplicate_gate),
)


def main() -> int:
    started = time.monotonic()
    results: list[dict[str, object]] = []
    ok = True
    for name, check in CHECKS:
        t0 = time.monotonic()
        try:
            check()
            results.append(
                {"name": name, "ok": True, "elapsed_ms": round((time.monotonic() - t0) * 1000, 2)}
            )
        except Exception as exc:  # noqa: BLE001 - structured diagnostic result
            ok = False
            results.append(
                {
                    "name": name,
                    "ok": False,
                    "elapsed_ms": round((time.monotonic() - t0) * 1000, 2),
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                }
            )
    print(
        json.dumps(
            {
                "ok": ok,
                "suite": "production-safe-v1",
                "checks": results,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
            },
            separators=(",", ":"),
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
