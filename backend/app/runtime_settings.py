from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings


@dataclass
class RuntimeSettings:
    auto_index_enabled: bool
    auto_index_interval_seconds: int
    reindex_errored_files: bool
    reindex_skipped_files: bool
    follow_shortcut_folders: bool
    gemini_file_search_search_enabled: bool
    search_parallel_variants_enabled: bool
    search_use_captions: bool
    search_rerank_enabled: bool


_runtime: RuntimeSettings | None = None


def _env_defaults() -> RuntimeSettings:
    settings = get_settings()
    return RuntimeSettings(
        auto_index_enabled=settings.auto_index_enabled,
        auto_index_interval_seconds=max(30, settings.auto_index_interval_seconds),
        reindex_errored_files=settings.reindex_errored_files,
        reindex_skipped_files=settings.reindex_skipped_files,
        follow_shortcut_folders=settings.follow_shortcut_folders,
        gemini_file_search_search_enabled=settings.gemini_file_search_search_enabled,
        search_parallel_variants_enabled=settings.search_parallel_variants_enabled,
        search_use_captions=settings.search_use_captions,
        search_rerank_enabled=settings.search_rerank_enabled,
    )


def set_runtime_settings(runtime: RuntimeSettings) -> None:
    global _runtime
    _runtime = runtime


def get_runtime_settings() -> RuntimeSettings:
    global _runtime
    if _runtime is None:
        _runtime = _env_defaults()
    return _runtime


def update_runtime_settings(
    *,
    auto_index_enabled: bool | None = None,
    auto_index_interval_seconds: int | None = None,
    reindex_errored_files: bool | None = None,
    reindex_skipped_files: bool | None = None,
    follow_shortcut_folders: bool | None = None,
    gemini_file_search_search_enabled: bool | None = None,
    search_parallel_variants_enabled: bool | None = None,
    search_use_captions: bool | None = None,
    search_rerank_enabled: bool | None = None,
) -> RuntimeSettings:
    runtime = get_runtime_settings()
    if auto_index_enabled is not None:
        runtime.auto_index_enabled = auto_index_enabled
    if auto_index_interval_seconds is not None:
        runtime.auto_index_interval_seconds = max(30, auto_index_interval_seconds)
    if reindex_errored_files is not None:
        runtime.reindex_errored_files = reindex_errored_files
    if reindex_skipped_files is not None:
        runtime.reindex_skipped_files = reindex_skipped_files
    if follow_shortcut_folders is not None:
        runtime.follow_shortcut_folders = follow_shortcut_folders
    if gemini_file_search_search_enabled is not None:
        runtime.gemini_file_search_search_enabled = gemini_file_search_search_enabled
    if search_parallel_variants_enabled is not None:
        runtime.search_parallel_variants_enabled = search_parallel_variants_enabled
    if search_use_captions is not None:
        runtime.search_use_captions = search_use_captions
    if search_rerank_enabled is not None:
        runtime.search_rerank_enabled = search_rerank_enabled
    return runtime
