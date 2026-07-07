from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings


@dataclass
class RuntimeSettings:
    auto_index_enabled: bool
    auto_index_interval_seconds: int


_runtime: RuntimeSettings | None = None


def get_runtime_settings() -> RuntimeSettings:
    global _runtime
    if _runtime is None:
        settings = get_settings()
        _runtime = RuntimeSettings(
            auto_index_enabled=settings.auto_index_enabled,
            auto_index_interval_seconds=max(30, settings.auto_index_interval_seconds),
        )
    return _runtime


def update_runtime_settings(
    *,
    auto_index_enabled: bool | None = None,
    auto_index_interval_seconds: int | None = None,
) -> RuntimeSettings:
    runtime = get_runtime_settings()
    if auto_index_enabled is not None:
        runtime.auto_index_enabled = auto_index_enabled
    if auto_index_interval_seconds is not None:
        runtime.auto_index_interval_seconds = max(30, auto_index_interval_seconds)
    return runtime
