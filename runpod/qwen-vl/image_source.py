"""Qwen worker image: GHCR tag, never Docker Hub."""
from __future__ import annotations

import sys
from pathlib import Path

_RUNPOD = Path(__file__).resolve().parents[1]
if str(_RUNPOD) not in sys.path:
    sys.path.insert(0, str(_RUNPOD))

from secure_image import (  # noqa: E402
    DEFAULT_QWEN_IMAGE,
    is_docker_hub_image,
    registry_auth_id,
    require_registry_auth,
    resolve_qwen_image,
)
from secure_image import assert_secure_image as _assert_secure_image  # noqa: E402


def assert_secure_image(image: str, *, example: str = DEFAULT_QWEN_IMAGE) -> str:
    return _assert_secure_image(image, example=example)


__all__ = [
    "DEFAULT_QWEN_IMAGE",
    "assert_secure_image",
    "is_docker_hub_image",
    "registry_auth_id",
    "require_registry_auth",
    "resolve_qwen_image",
]
