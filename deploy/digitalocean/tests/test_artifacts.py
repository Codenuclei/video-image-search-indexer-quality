"""Lint-style checks for DigitalOcean Carousel deploy artifacts (no cloud calls)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DO = REPO / "deploy" / "digitalocean"
FRONTEND = REPO / ".do" / "frontend.yaml"
OBSOLETE_APP = REPO / ".do" / "app.yaml"


@pytest.fixture(scope="module")
def frontend_yaml() -> str:
    return FRONTEND.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose_yaml() -> str:
    return (DO / "docker-compose.yml").read_text(encoding="utf-8")


def test_required_files_exist():
    for path in (
        FRONTEND,
        DO / "docker-compose.yml",
        DO / "Caddyfile",
        DO / ".env.example",
        DO / "README.md",
        DO / "scripts" / "backup.sh",
        DO / "scripts" / "restore.sh",
        DO / "scripts" / "preflight.sh",
        DO / "scripts" / "deploy-backend.sh",
    ):
        assert path.is_file(), path
    assert not OBSOLETE_APP.exists(), "obsolete .do/app.yaml must be removed"


def test_scripts_bash_syntax():
    for name in ("backup.sh", "restore.sh", "preflight.sh", "deploy-backend.sh"):
        script = DO / "scripts" / name
        proc = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"{name}: {proc.stderr}"


def _service_names(text: str) -> list[str]:
    return re.findall(r"^\s+-\s+name:\s+(\S+)\s*$", text, flags=re.M)


def test_frontend_spec_single_service(frontend_yaml: str):
    assert "registry_type: DOCR" in frontend_yaml
    assert "repository: dfi-carousel-frontend" in frontend_yaml
    assert "tag: REPLACE_IMAGE_TAG" in frontend_yaml
    assert "http_port: 3002" in frontend_yaml
    assert "https://api-carousel.139-59-35-242.sslip.io" in frontend_yaml
    assert "API_PROXY_TARGET" in frontend_yaml
    assert "NEXT_PUBLIC_BACKEND_URL" in frontend_yaml
    assert "REPLACE_WITH_VPC_UUID" in frontend_yaml
    assert not re.search(r"\$\{[^}]*PRIVATE_URL\}", frontend_yaml)
    names = _service_names(frontend_yaml)
    assert names == ["carousel-frontend"], names
    assert "carousel-backend" not in frontend_yaml
    for banned in ("dfi-backend", "dfi-frontend", "dfi-face-worker"):
        assert f"name: {banned}" not in frontend_yaml


def test_env_types_uppercase(frontend_yaml: str):
    types = re.findall(r"^\s+type:\s+(\S+)\s*$", frontend_yaml, flags=re.M)
    assert types, "frontend: no type: fields"
    assert all(t in {"GENERAL", "SECRET"} for t in types)
    assert "type: general" not in frontend_yaml
    assert "type: secret" not in frontend_yaml
    assert "EV[1:" not in frontend_yaml


def test_compose_private_pinned_stack(compose_yaml: str):
    assert "pgvector/pgvector:pg17" in compose_yaml
    assert re.search(r"qdrant/qdrant:v\d+\.\d+\.\d+", compose_yaml)
    assert "DROPLET_BIND_ADDR" in compose_yaml
    assert "restart: unless-stopped" in compose_yaml
    assert compose_yaml.count("healthcheck:") >= 3
    assert "carousel_pg_data" in compose_yaml
    assert "carousel_qdrant_storage" in compose_yaml
    assert "carousel-backend:" in compose_yaml
    assert "caddy:" in compose_yaml
    assert "CAROUSEL_BACKEND_IMAGE" in compose_yaml
    assert "/opt/dfi-carousel/backend-data" in compose_yaml
    assert ":/app/data" in compose_yaml


def test_caddy_routes_public_backend_domain():
    caddy = (DO / "Caddyfile").read_text(encoding="utf-8")
    assert "api-carousel.139-59-35-242.sslip.io" in caddy
    assert "reverse_proxy carousel-backend:8000" in caddy


def test_backup_script_covers_all_collections():
    text = (DO / "scripts" / "backup.sh").read_text(encoding="utf-8")
    for coll in (
        "dfi_video_frames",
        "dfi_images",
        "dfi_image_captions",
        "dfi_video_transcripts",
    ):
        assert coll in text


def test_env_example_has_no_filled_secrets():
    for line in (DO / ".env.example").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key in {
            "POSTGRES_PASSWORD",
            "SPACES_ACCESS_KEY_ID",
            "SPACES_SECRET_ACCESS_KEY",
            "BACKUP_ENCRYPTION_PASSPHRASE",
        }:
            assert val == "", f"{key} must be empty in .env.example"


def test_preflight_script_passes():
    script = DO / "scripts" / "preflight.sh"
    script.chmod(script.stat().st_mode | 0o111)
    (DO / "scripts" / "backup.sh").chmod(
        (DO / "scripts" / "backup.sh").stat().st_mode | 0o111
    )
    (DO / "scripts" / "restore.sh").chmod(
        (DO / "scripts" / "restore.sh").stat().st_mode | 0o111
    )
    proc = subprocess.run(
        ["bash", str(script)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr


def test_readme_documents_async_jobs():
    readme = (DO / "README.md").read_text(encoding="utf-8")
    assert "100 seconds" in readme
    assert "carousel_studio_jobs" in readme
    assert "select-images/status" in readme
    assert "themes/jobs" in readme
    assert "extract/status" in readme
    assert "generate/status" in readme
    assert "WEB_CONCURRENCY" in readme
