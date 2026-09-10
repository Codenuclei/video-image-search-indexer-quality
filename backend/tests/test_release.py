from __future__ import annotations

from app.release import release_info


def test_release_info_is_sanitized(monkeypatch) -> None:
    monkeypatch.setenv("GIT_SHA", "abc123")
    monkeypatch.setenv("IMAGE_DIGEST", "sha256:deadbeef")
    monkeypatch.setenv("RUN_INDEXER", "false")
    monkeypatch.setenv("RUNPOD_API_KEY", "should-not-appear")
    info = release_info()
    assert info == {
        "git_sha": "abc123",
        "image_digest": "sha256:deadbeef",
        "role": "api",
    }
    assert "should-not-appear" not in info.values()


def test_release_info_indexer_role(monkeypatch) -> None:
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("IMAGE_DIGEST", raising=False)
    monkeypatch.setenv("RUN_INDEXER", "true")
    info = release_info()
    assert info["role"] == "indexer"
    assert info["git_sha"] == "unknown"
    assert info["image_digest"] == "unknown"
