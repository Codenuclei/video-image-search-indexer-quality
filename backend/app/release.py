"""Sanitized release identity for DigitalOcean (and any other host)."""
from __future__ import annotations

import os


def release_info() -> dict[str, str]:
    """Git SHA, image digest, and process role. No secrets."""
    run_indexer = (os.environ.get("RUN_INDEXER") or "").strip().lower() in {"1", "true", "yes"}
    return {
        "git_sha": (os.environ.get("GIT_SHA") or os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "unknown").strip()
        or "unknown",
        "image_digest": (os.environ.get("IMAGE_DIGEST") or "unknown").strip() or "unknown",
        "role": "indexer" if run_indexer else "api",
    }
