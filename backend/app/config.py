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
    # Uploading images to the Gemini File Search store is redundant and consumes
    # the shared 10GB store quota, so it is disabled by default.
    gemini_file_search_images_enabled: bool = False
    gemini_file_search_search_enabled: bool = False
    # Parallel embed+Qdrant per query variant — off by default (can hurt quality under API load).
    search_parallel_variants_enabled: bool = False
    search_variant_max_parallel: int = 0   # 0 = auto (cpu cores); parallel variant embed+Qdrant
    search_llm_batch_parallel: int = 0     # 0 = auto; caption filter + rerank batch concurrency
    cpu_thread_pool_size: int = 0          # 0 = os.cpu_count()
    image_index_max_parallel: int = 6      # concurrent image index jobs (face detect + embed)

    # Image caption/embedding backfill throughput (maintenance loop).
    image_caption_batch_parallel: int = 6   # concurrent Gemini describe batches
    image_embed_backfill_parallel: int = 6  # concurrent image embedding upserts
    maintenance_batches_per_tick: int = 6   # max caption batches per maintenance tick

    # Search UI defaults (persisted in app_settings table).
    search_use_captions: bool = False
    search_rerank_enabled: bool = True
    # Append-only caption-text LLM filter (no images sent to Gemini).
    search_caption_filter_enabled: bool = True
    search_caption_filter_pool_size: int = 120
    search_caption_filter_batch_size: int = 25
    search_caption_filter_parallel: int = 0
    search_caption_filter_gap_seconds: float = 0.4

    auto_index_enabled: bool = False
    auto_index_interval_seconds: int = 30
    reindex_errored_files: bool = False
    reindex_skipped_files: bool = False

    # Follow Google Drive folder shortcuts when listing/syncing the connected tree.
    follow_shortcut_folders: bool = True
    # Experimental Library overlay to manually name individual faces. Off by default.
    experimental_manual_face_tag: bool = False

    # TIFF/RAW decode: max attempts before permanent skip (stops infinite requeue loops).
    decode_max_attempts: int = 1

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
    # Only face clusters at or above this detection confidence appear in the review queue.
    review_queue_min_confidence: float = 0.80

    # Video indexing (ffmpeg frames + VTT transcript + Gemini VLM)
    video_indexing_enabled: bool = True
    video_cache_dir: str = "./data/videos"
    video_frame_interval_seconds: float = 1.0
    video_max_sample_frames: int = 300
    video_max_gemini_frames: int = 12
    video_vlm_enrich: bool = True
    # Max videos indexing at the same time (each uses the same pipeline).
    video_index_max_parallel: int = 3

    # Gemini API client-side concurrency (tune to your tier; see ai.google.dev rate limits).
    # Embedding 2: high RPM (~40k) — safe default 24 concurrent frame embeds.
    # Flash VLM + File Search uploads: lower — defaults 6 / 4.
    gemini_embed_max_concurrent: int = 24
    gemini_vlm_max_concurrent: int = 6
    gemini_upload_max_concurrent: int = 4

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
    gemini_video_min_score: float = 0.25
    gemini_video_display_min_score: float = 0.32   # cosine threshold — lower = more recall
    gemini_image_result_limit: int = 30
    gemini_image_min_score: float = 0.25

    # Query expansion (LLM rewrites → multi-vector fusion) for higher recall
    search_query_expansion: bool = True

    # Image captioning: index-time VLM description → caption text embedding.
    # Search compares the query against captions (text→text, well-calibrated),
    # which filters vague visual matches without slowing search.
    image_caption_enabled: bool = True
    image_caption_model: str = "gemini-2.5-flash"
    image_caption_max_dim: int = 512          # downscale longest side before VLM
    image_caption_batch_size: int = 8         # images per Gemini describe call
    image_caption_min_words: int = 4          # captions shorter than this are re-generated
    qdrant_image_captions_collection: str = "dfi_image_captions"
    # Fusion of visual (image-embedding) and caption (text-embedding) cosine.
    image_visual_weight: float = 0.4
    image_caption_weight: float = 0.6
    image_caption_min_score: float = 0.55     # caption text-match precision gate
    image_visual_strong_score: float = 0.50   # keep on strong visual alone

    # Append-only body/clothing re-id layer (body_signatures table).
    reid_enabled: bool = True
    reid_min_face_area_fraction: float = 0.015   # face must be prominent in frame
    reid_min_body_coverage: float = 0.55         # ≥55% of expected full-body extent visible
    reid_body_match_threshold: float = 0.60      # cosine similarity for candidate links
    reid_backfill_max_parallel: int = 4

    # Append-only reverse-image / people-web identification (face_web_matches).
    # Preferred free path: Cohesivity Exa people search (image → Gemini clues → Exa).
    # Optional paid fallback: SERPAPI_KEY for true Google Lens reverse image search.
    # Free Google reverse image (SOME-1HING style). Hosted API first, scrape fallback.
    google_reverse_api_url: str = "https://google-reverse-image-api.vercel.app/reverse"
    # Official Google Cloud Vision Web Detection reverse-image API.
    # Enable Cloud Vision API + billing on the key's Google Cloud project.
    google_vision_api_key: str = ""
    cohesivity_application_key: str = ""
    cohesivity_exa_base_url: str = "https://cohesivity.ai/edge/exa-api"
    serpapi_key: str = ""
    # Public base URL so Google can fetch /faces/{id}/thumbnail for reverse search.
    public_base_url: str = ""

    # Qwen3-VL sidecar (OpenAI-compatible vLLM) for local frame captioning
    qwen_vlm_enabled: bool = False
    qwen_vlm_base_url: str = "http://127.0.0.1:8003"
    qwen_vlm_model: str = "Qwen/Qwen3-VL-8B-Instruct"
    qwen_vlm_timeout_seconds: float = 120.0
    qwen_vlm_max_tokens: int = 256


@lru_cache
def get_settings() -> Settings:
    return Settings()
