"""RunPod worker images must come from GHCR, not Docker Hub."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "runpod"))
sys.path.insert(0, str(REPO / "runpod" / "qwen-vl"))

from image_source import (  # noqa: E402
    DEFAULT_QWEN_IMAGE,
    assert_secure_image,
    is_docker_hub_image,
    require_registry_auth,
    resolve_qwen_image,
)
from secure_image import DEFAULT_FACE_IMAGE, resolve_face_image  # noqa: E402


@pytest.mark.parametrize(
    "image, hub",
    [
        ("", True),
        ("lmsysorg/sglang:dev-cu12", True),
        ("vllm/vllm-openai:latest", True),
        ("runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04", True),
        ("docker.io/lmsysorg/sglang:dev-cu12", True),
        ("nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04", True),
        (DEFAULT_QWEN_IMAGE, False),
        (DEFAULT_FACE_IMAGE, False),
        ("ghcr.io/codenuclei/video-image-search-indexer-quality/dfi-qwen3-vl-sglang:gpu", False),
        ("nvcr.io/nvidia/pytorch:24.01-py3", False),
    ],
)
def test_is_docker_hub_image(image: str, hub: bool) -> None:
    assert is_docker_hub_image(image) is hub


def test_assert_secure_image_rejects_hub() -> None:
    with pytest.raises(ValueError, match="Docker Hub"):
        assert_secure_image("lmsysorg/sglang:dev-cu12")


def test_resolve_defaults_are_ghcr() -> None:
    assert resolve_qwen_image({}).startswith("ghcr.io/")
    assert resolve_face_image({}).startswith("ghcr.io/")


def test_resolve_rejects_override_to_hub() -> None:
    with pytest.raises(ValueError, match="Docker Hub"):
        resolve_qwen_image({"RUNPOD_QWEN_IMAGE": "lmsysorg/sglang:dev-cu12"})
    with pytest.raises(ValueError, match="Docker Hub"):
        resolve_face_image({"RUNPOD_FACE_IMAGE": "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"})


def test_require_registry_auth_for_ghcr() -> None:
    with pytest.raises(ValueError, match="RUNPOD_CONTAINER_REGISTRY_AUTH_ID"):
        require_registry_auth(DEFAULT_QWEN_IMAGE, {})
    assert require_registry_auth(DEFAULT_QWEN_IMAGE, {"RUNPOD_CONTAINER_REGISTRY_AUTH_ID": "abc"}) == "abc"


def test_create_scripts_do_not_embed_docker_hub_tags() -> None:
    scripts = [
        REPO / "scripts" / "runpod_create_qwen_endpoint.py",
        REPO / "scripts" / "runpod_create_face_endpoint.py",
        REPO / "scripts" / "runpod_qwen_sglang_identify.py",
        REPO / "scripts" / "runpod_qwen_vl_load_test.py",
    ]
    forbidden = (
        "lmsysorg/sglang",
        "vllm/vllm-openai",
        "runpod/pytorch",
        "raw.githubusercontent.com",
        "docker.io/",
    )
    for path in scripts:
        text = path.read_text()
        for needle in forbidden:
            assert needle not in text, f"{path.name} still references {needle}"
