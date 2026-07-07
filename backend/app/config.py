from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for Drive → Gemini File Search RAG."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://drivefaceindexer:drivefaceindexer@localhost:5432/drivefaceindexer"

    # Google OAuth (replaces the external Drive Connector)
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    google_api_key: str = ""

    # URL of the Next.js frontend — used to redirect after OAuth
    frontend_url: str = "http://localhost:3001"

    # Comma-separated list of extra CORS origins (e.g. Railway frontend domain)
    allowed_origins: str = ""

    # Legacy Drive Connector HTTP settings (kept so existing env files don't break)
    drive_connector_base_url: str = "http://localhost:3000"
    drive_connector_api_key: str = ""
    drive_connector_timeout_seconds: float = 900.0

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_embedding_model: str = "models/gemini-embedding-2"
    gemini_file_search_store_display_name: str = "drive-connector-shared"
    gemini_upload_poll_seconds: float = 3.0
    gemini_upload_timeout_seconds: float = 600.0
    # Images are searched via Qdrant vector embeddings (Gemini Embedding 2).
    # Uploading them to the Gemini File Search store is redundant and consumes
    # the shared 10GB store quota, so it is disabled by default.
    gemini_file_search_images_enabled: bool = False

    auto_index_enabled: bool = False
    auto_index_interval_seconds: int = 30

    webhook_secret: str = ""

    temp_dir: str = "./data/tmp"
    thumbnail_dir: str = "./data/thumbnails"
    max_media_bytes_in_memory: int = 8 * 1024 * 1024

    # InsightFace / face detection pipeline
    insightface_model_name: str = "buffalo_l"
    insightface_providers: list[str] = ["CPUExecutionProvider"]
    face_detection_size: tuple[int, int] = (640, 640)
    min_detection_confidence: float = 0.5
    min_face_area_fraction: float = 0.001
    media_dedup_similarity_threshold: float = 0.85
    person_match_threshold: float = 0.6

    # Video indexing (ffmpeg frames + VTT transcript + Gemini VLM)
    video_indexing_enabled: bool = True
    video_cache_dir: str = "./data/videos"
    video_frame_interval_seconds: float = 1.0
    video_max_sample_frames: int = 300
    video_max_gemini_frames: int = 12
    video_vlm_enrich: bool = True

    # Legacy Fennec sidecar (disabled — use video_indexing_enabled instead)
    fennec_enabled: bool = False
    fennec_base_url: str = "http://127.0.0.1:8002"
    fennec_video_cache_dir: str = "./data/fennec-media"
    fennec_timeout_seconds: float = 900.0

    # Semantic Video Search (SVS) — legacy SigLIP container (disabled; replaced by Gemini Embedding 2)
    svs_enabled: bool = False
    svs_base_url: str = "http://localhost:8001"
    svs_user_id: str = ""
    svs_timeout_seconds: float = 900.0
    svs_result_limit: int = 10
    svs_min_score: float = 0.0

    # Gemini Embedding 2 — frame-level video search via Qdrant
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_collection: str = "dfi_video_frames"
    qdrant_images_collection: str = "dfi_images"
    gemini_video_result_limit: int = 30   # Qdrant candidates before re-rank
    gemini_video_min_score: float = 0.25   # cosine threshold — lower = more recall
    gemini_image_result_limit: int = 30
    gemini_image_min_score: float = 0.25

    # Query expansion (LLM rewrites → multi-vector fusion) for higher recall
    search_query_expansion: bool = True

    # Qwen3-VL sidecar (OpenAI-compatible vLLM) for local frame captioning
    qwen_vlm_enabled: bool = False
    qwen_vlm_base_url: str = "http://127.0.0.1:8003"
    qwen_vlm_model: str = "Qwen/Qwen3-VL-8B-Instruct"
    qwen_vlm_timeout_seconds: float = 120.0
    qwen_vlm_max_tokens: int = 256


@lru_cache
def get_settings() -> Settings:
    return Settings()
