"""RunPod workers must pull a private registry image, not Docker Hub.

Build machines may still `FROM` a Hub base. Workers download the baked GHCR
tag over an authenticated registry URL.
"""
from __future__ import annotations

import os

DEFAULT_QWEN_IMAGE = (
    "ghcr.io/codenuclei/video-image-search-indexer-quality/dfi-qwen3-vl-sglang:gpu"
)
DEFAULT_FACE_IMAGE = (
    "ghcr.io/codenuclei/video-image-search-indexer-quality/dfi-face-buffalo:gpu"
)


def is_docker_hub_image(image: str) -> bool:
    name = (image or "").strip().casefold()
    if not name:
        return True
    if name.startswith("docker.io/") or name.startswith("index.docker.io/"):
        return True
    if name.startswith(("ghcr.io/", "nvcr.io/", "quay.io/")):
        return False
    # Hosted registries include a dot in the first path segment (ghcr.io/...).
    if "." in name.split("/")[0]:
        return False
    return True


def assert_secure_image(image: str, *, example: str) -> str:
    """Raise if this would pull a public Docker Hub tag at worker start."""
    name = (image or "").strip()
    if is_docker_hub_image(name):
        raise ValueError(
            f"Refusing Docker Hub image {name!r}. "
            "Set a GHCR (or other private registry) tag such as "
            f"{example}."
        )
    return name


def resolve_qwen_image(environ: dict[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    return assert_secure_image(
        env.get("RUNPOD_QWEN_IMAGE") or DEFAULT_QWEN_IMAGE,
        example=DEFAULT_QWEN_IMAGE,
    )


def resolve_face_image(environ: dict[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    return assert_secure_image(
        env.get("RUNPOD_FACE_IMAGE") or DEFAULT_FACE_IMAGE,
        example=DEFAULT_FACE_IMAGE,
    )


def registry_auth_id(environ: dict[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    return (env.get("RUNPOD_CONTAINER_REGISTRY_AUTH_ID") or "").strip()


def require_registry_auth(image: str, environ: dict[str, str] | None = None) -> str:
    auth_id = registry_auth_id(environ)
    if image.startswith("ghcr.io/") and not auth_id:
        raise ValueError(
            "RUNPOD_CONTAINER_REGISTRY_AUTH_ID is required so workers pull GHCR "
            "with a private token, not Docker Hub."
        )
    return auth_id
