from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class DriveFileOut(BaseModel):
    id: str
    name: str
    mime_type: str
    path: str
    status: str
    size: int | None = None
    modified_time: datetime | None = None
    last_synced_at: datetime | None = None
    error_message: str | None = None
    source: str = "drive"

    model_config = {"from_attributes": True}


class IndexRunResult(BaseModel):
    discovered: int = 0
    processed: int = 0
    skipped: int = 0
    errored: int = 0
    deferred: int = 0


class IndexLaneSlots(BaseModel):
    active: int = 0
    max: int = 0


class IndexStatus(BaseModel):
    is_running: bool
    counts_by_status: dict[str, int]
    last_run: IndexRunResult | None = None
    last_run_at: datetime | None = None
    current_file: str | None = None
    current_files: list[str] = []
    # Per-lane lists so UI does not mix image filenames with video jobs.
    current_image_files: list[str] = []
    current_video_files: list[str] = []
    image_slots: IndexLaneSlots = IndexLaneSlots()
    video_slots: IndexLaneSlots = IndexLaneSlots()
    active_image_jobs: int = 0
    active_video_jobs: int = 0
    auto_index_enabled: bool = False
    auto_index_interval_seconds: int = 30
    pending_count: int = 0
    go_indexer_enabled: bool = False
    go_indexer_alive: bool = False
    go_files_per_sec: float | None = None


class GoIndexerClaimItem(BaseModel):
    id: str
    name: str
    mime_type: str | None = None
    size: int | None = None
    path: str | None = None


class GoIndexerClaimResponse(BaseModel):
    enabled: bool
    items: list[GoIndexerClaimItem]
    max_parallel: int
    canary_limit: int


class GoIndexerStatusOut(BaseModel):
    enabled: bool
    alive: bool
    last_heartbeat_at: datetime | None = None
    max_parallel: int
    canary_limit: int
    claimed_open: int
    last_files_ok: int = 0
    last_files_err: int = 0
    last_elapsed_ms: int = 0
    last_files_per_sec: float = 0.0
    last_download_bytes: int = 0
    last_reported_at: datetime | None = None


class GoIndexerReportIn(BaseModel):
    files_ok: int = 0
    files_err: int = 0
    elapsed_ms: int = 0
    download_bytes: int = 0


class SettingsOut(BaseModel):
    gemini_model: str
    gemini_file_search_store_display_name: str
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
    go_indexer_enabled: bool


class SettingsUpdate(BaseModel):
    auto_index_enabled: bool | None = None
    auto_index_interval_seconds: int | None = None
    reindex_errored_files: bool | None = None
    reindex_skipped_files: bool | None = None
    follow_shortcut_folders: bool | None = None
    experimental_manual_face_tag: bool | None = None
    gemini_file_search_search_enabled: bool | None = None
    search_parallel_variants_enabled: bool | None = None
    search_use_captions: bool | None = None
    search_rerank_enabled: bool | None = None
    go_indexer_enabled: bool | None = None


class FaceOut(BaseModel):
    id: int
    media_id: int
    bbox_x: float
    bbox_y: float
    bbox_width: float
    bbox_height: float
    detection_confidence: float
    frame_timestamp: float | None = None
    page_number: int | None = None
    cluster_id: int | None = None
    person_id: int | None = None
    has_thumbnail: bool = False

    model_config = {"from_attributes": True}


class MediaOccurrence(BaseModel):
    media_id: int
    drive_file_id: str
    name: str
    path: str
    media_type: str
    frame_timestamp: float | None = None


class ClusterOut(BaseModel):
    id: int
    status: str
    member_count: int
    representative_face_id: int | None
    representative_confidence: float | None = None
    appears_in: list[MediaOccurrence]
    created_at: datetime

    model_config = {"from_attributes": True}


class ClusterListResponse(BaseModel):
    items: list[ClusterOut]
    total: int
    offset: int
    limit: int


class NameClusterRequest(BaseModel):
    name: str


class TagFaceRequest(BaseModel):
    """Manual face tag (experimental mode) — names a single face only."""

    name: str


class ManualFaceBoxRequest(BaseModel):
    """Draw a freeform box on an image and optionally name it (experimental)."""

    drive_file_id: str
    bbox_x: float
    bbox_y: float
    bbox_width: float
    bbox_height: float
    name: str | None = None


class RenamePersonRequest(BaseModel):
    name: str


class UpdatePersonRequest(BaseModel):
    name: str | None = None
    role: str | None = None  # student | non_student | null to clear


class MergeClusterRequest(BaseModel):
    person_id: int


class PersonOut(BaseModel):
    id: int
    name: str
    role: str | None = None
    representative_face_id: int | None
    occurrence_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class MediaOut(BaseModel):
    id: int
    drive_file_id: str
    type: str
    page_count: int | None
    duration_seconds: float | None
    face_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class SearchCitationOut(BaseModel):
    file_name: str | None = None
    source: str | None = None
    drive_file_id: str | None = None
    drive_path: str | None = None
    metadata: dict[str, str] = {}


class SearchResultFile(BaseModel):
    drive_file_id: str
    name: str
    path: str
    mime_type: str
    person_names: list[str] = []
    score: float | None = None
    caption: str | None = None


class SearchMoment(BaseModel):
    drive_file_id: str
    name: str
    path: str
    mime_type: str
    timestamp_sec: float
    end_timestamp_sec: float | None = None
    match_type: str
    fennec_scene_id: int | None = None
    preview_url: str
    video_url: str | None = None
    person_names: list[str] = []
    snippet: str | None = None
    score: float | None = None


class SearchResponse(BaseModel):
    query: str
    answer: str
    citations: list[SearchCitationOut]
    files: list[SearchResultFile] = []
    moments: list[SearchMoment] = []
