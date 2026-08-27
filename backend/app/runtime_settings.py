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
    experimental_manual_face_tag: bool
    gemini_file_search_search_enabled: bool
    search_parallel_variants_enabled: bool
    search_use_captions: bool
    search_rerank_enabled: bool
    search_semantic_min_score: float
    go_indexer_enabled: bool
    carousel_llm_provider: str
    openrouter_model: str
    claude_model: str
    object_lane_enabled: bool = False
    object_backfill_enabled: bool = False
    object_confidence_floor: float = 0.72
    object_max_labels: int = 12
    object_batch_size: int = 8
    object_face_priority_ratio: int = 10


_runtime: RuntimeSettings | None = None

_VALID_CAROUSEL_LLM_PROVIDERS = frozenset({"auto", "openrouter", "claude", "gemini"})


def _normalize_provider(value: str | None) -> str:
    raw = (value or "auto").strip().lower()
    return raw if raw in _VALID_CAROUSEL_LLM_PROVIDERS else "auto"


def _env_defaults() -> RuntimeSettings:
    settings = get_settings()
    return RuntimeSettings(
        auto_index_enabled=settings.auto_index_enabled,
        auto_index_interval_seconds=max(30, settings.auto_index_interval_seconds),
        reindex_errored_files=settings.reindex_errored_files,
        reindex_skipped_files=settings.reindex_skipped_files,
        follow_shortcut_folders=settings.follow_shortcut_folders,
        experimental_manual_face_tag=settings.experimental_manual_face_tag,
        gemini_file_search_search_enabled=settings.gemini_file_search_search_enabled,
        search_parallel_variants_enabled=settings.search_parallel_variants_enabled,
        search_use_captions=settings.search_use_captions,
        search_rerank_enabled=settings.search_rerank_enabled,
        search_semantic_min_score=max(0.0, min(1.0, settings.image_caption_min_score)),
        go_indexer_enabled=settings.go_indexer_enabled,
        object_lane_enabled=False,
        object_backfill_enabled=False,
        object_confidence_floor=0.72,
        object_max_labels=12,
        object_batch_size=8,
        object_face_priority_ratio=10,
        # Default Claude-direct when Anthropic key is present; picker can override.
        carousel_llm_provider="claude"
        if (settings.anthropic_api_key or settings.claude_api_key or "").strip()
        else "auto",
        openrouter_model=(settings.openrouter_model or "anthropic/claude-sonnet-4").strip(),
        claude_model=(settings.claude_model or "claude-sonnet-4-5-20250929").strip(),
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
    experimental_manual_face_tag: bool | None = None,
    gemini_file_search_search_enabled: bool | None = None,
    search_parallel_variants_enabled: bool | None = None,
    search_use_captions: bool | None = None,
    search_rerank_enabled: bool | None = None,
    search_semantic_min_score: float | None = None,
    go_indexer_enabled: bool | None = None,
    object_lane_enabled: bool | None = None,
    object_backfill_enabled: bool | None = None,
    object_confidence_floor: float | None = None,
    object_max_labels: int | None = None,
    object_batch_size: int | None = None,
    object_face_priority_ratio: int | None = None,
    carousel_llm_provider: str | None = None,
    openrouter_model: str | None = None,
    claude_model: str | None = None,
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
    if experimental_manual_face_tag is not None:
        runtime.experimental_manual_face_tag = experimental_manual_face_tag
    if gemini_file_search_search_enabled is not None:
        runtime.gemini_file_search_search_enabled = gemini_file_search_search_enabled
    if search_parallel_variants_enabled is not None:
        runtime.search_parallel_variants_enabled = search_parallel_variants_enabled
    if search_use_captions is not None:
        runtime.search_use_captions = search_use_captions
    if search_rerank_enabled is not None:
        runtime.search_rerank_enabled = search_rerank_enabled
    if search_semantic_min_score is not None:
        runtime.search_semantic_min_score = max(0.0, min(1.0, search_semantic_min_score))
    if go_indexer_enabled is not None:
        runtime.go_indexer_enabled = go_indexer_enabled
    if object_lane_enabled is not None:
        runtime.object_lane_enabled = object_lane_enabled
    if object_backfill_enabled is not None:
        runtime.object_backfill_enabled = object_backfill_enabled
    if object_confidence_floor is not None:
        runtime.object_confidence_floor = max(0.0, min(1.0, object_confidence_floor))
    if object_max_labels is not None:
        runtime.object_max_labels = max(1, min(50, object_max_labels))
    if object_batch_size is not None:
        runtime.object_batch_size = max(1, min(64, object_batch_size))
    if object_face_priority_ratio is not None:
        runtime.object_face_priority_ratio = max(1, min(100, object_face_priority_ratio))
    if carousel_llm_provider is not None:
        runtime.carousel_llm_provider = _normalize_provider(carousel_llm_provider)
    if openrouter_model is not None:
        cleaned = openrouter_model.strip()
        if cleaned:
            runtime.openrouter_model = cleaned
    if claude_model is not None:
        cleaned = claude_model.strip()
        if cleaned:
            runtime.claude_model = cleaned
    return runtime
