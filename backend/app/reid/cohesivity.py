"""Resolve Cohesivity application key from env or the project `.cohesivity` file."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


def _parse_dotfile(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


@lru_cache(maxsize=1)
def cohesivity_application_key() -> str:
    """
    Prefer COHESIVITY_APPLICATION_KEY / Settings, then walk up from CWD / this
    file for a `.cohesivity` written by genesis.
    """
    settings = get_settings()
    if settings.cohesivity_application_key:
        return settings.cohesivity_application_key.strip()

    candidates = [
        Path.cwd() / ".cohesivity",
        Path.cwd().parent / ".cohesivity",
        Path(__file__).resolve().parents[3] / ".cohesivity",  # repo root from app/
        Path(__file__).resolve().parents[2] / ".cohesivity",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            key = _parse_dotfile(path).get("coh_application_key", "").strip()
        except OSError as exc:
            logger.warning("Could not read %s: %s", path, exc)
            continue
        if key:
            return key
    return ""
