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
    # Browser API key for Google Picker (setDeveloperKey). Not a Gemini / AI Studio key.
    google_api_key: str = ""

    # URL of the Next.js frontend — used to redirect after OAuth
    frontend_url: str = "http://localhost:3001"
    # Carousel studio frontend — OAuth return_to allowlist + preferred base for /carousel|/test
    carousel_frontend_url: str = "http://localhost:3002"

    # Comma-separated list of extra CORS origins (e.g. Railway frontend domain)
    allowed_origins: str = ""

    # Legacy Drive Connector HTTP settings (kept so existing env files don't break)
    drive_connector_base_url: str = "http://localhost:3000"
    drive_connector_api_key: str = ""
    drive_connector_timeout_seconds: float = 900.0

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    # Optional Claude copy/theme writer; Gemini remains the image-ranking provider.
    anthropic_api_key: str = ""
    claude_api_key: str = ""
    claude_model: str = "claude-sonnet-4-5-20250929"
    # OpenRouter (carousel LLM) — key is env-only; model/provider are runtime-editable.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "anthropic/claude-sonnet-4"
    gemini_embedding_model: str = "models/gemini-embedding-2"
    gemini_file_search_store_display_name: str = "drive-connector-shared"
    gemini_upload_poll_seconds: float = 3.0
    gemini_upload_timeout_seconds: float = 600.0
    # Images are searched via Qdrant vector embeddings (Gemini Embedding 2).
    # Google File Search store is retired — local Qdrant RAG only (frames/images/captions/transcripts).
    gemini_file_search_images_enabled: bool = False
    gemini_file_search_search_enabled: bool = False
    # Hard kill: never create/upload/query Google fileSearchStores (keep VLM/embed APIs).
    gemini_file_search_disabled: bool = True
    # Parallel embed+Qdrant per query variant — off by default (can hurt quality under API load).
    search_parallel_variants_enabled: bool = False
    search_variant_max_parallel: int = 0   # 0 = auto (cpu cores); parallel variant embed+Qdrant
    search_llm_batch_parallel: int = 0     # 0 = auto; caption filter + rerank batch concurrency
    cpu_thread_pool_size: int = 0          # 0 = os.cpu_count()
    # Concurrent image jobs must fit under one worker's DB pool
    # (pool_size + max_overflow). 46 parallel jobs with pool≈24 caused mass
    # QueuePool timeouts → fake "failed" spike. Keep ≤ ~half the pool.
    # Keep low: high values + InsightFace/ONNX caused "can't start new thread".
    image_index_max_parallel: int = 8
    # Shared Drive download concurrency (indexer + maintenance + preview).
    # Keep in lockstep with image slots so Active jobs are not download-starved.
    drive_download_max_concurrent: int = 8
    # Cap simultaneous DB sessions held by image index jobs (download/face/embed).
    # Status finals are batched separately — see index_status_batch_size.
    index_db_max_concurrent: int = 8
    # Flush PROCESSED/ERROR/PENDING status writes in one UPDATE every N files.
    index_status_batch_size: int = 100
    # Parallel Drive folder children listings during recursive tree walk.
    drive_list_max_concurrent: int = 4
    # Optional Go sidecar canary (claim/download in Go, complete via Python ingest).
    go_indexer_enabled: bool = False
    go_indexer_max_parallel: int = 2
    go_indexer_canary_limit: int = 20
    go_indexer_heartbeat_seconds: float = 60.0
    go_indexer_claim_stall_seconds: float = 900.0
    # Prefer smaller pending files first so the queue keeps moving.
    index_prefer_small_files: bool = True
    # How many pending rows to scan per claim round (multiplied by free slots).
    index_claim_window_multiplier: int = 40
    # Orphan PROCESSING videos (no live task) become ERROR after this many seconds.
    # Live Whisper/index tasks are never cancelled by this watchdog.
    video_index_stall_seconds: int = 3600

    # Dedicated indexer service sets RUN_INDEXER=true with WEB_CONCURRENCY=1.
    # API-only replicas set RUN_INDEXER=false so search stays responsive.
    # Default true keeps a single-service deploy indexing until the split.
    run_indexer: bool = True
    # When true, image indexer enqueues Postgres face_jobs instead of inline InsightFace.
    # Pair with RUN_FACE_WORKER=true on volume-less dfi-face-worker replicas.
    face_jobs_enabled: bool = False
    # Face-only consumer (dfi-face-worker): claim face_jobs via SKIP LOCKED.
    # Keep WEB_CONCURRENCY=1 and FACE_WORKER_CONCURRENCY=1 (sequential InsightFace lock).
    run_face_worker: bool = False
    face_worker_concurrency: int = 1
    face_job_lease_seconds: int = 900
    face_job_max_attempts: int = 3
    # Stop claiming new index downloads when free space on media/video volume is below this.
    index_disk_high_water_bytes: int = 2 * 1024 * 1024 * 1024

    # Image caption/embedding backfill throughput (maintenance loop).
    # Caption ~50/s: batch 10 × parallel ≈ 50*latency_s/10 (probe: ~13s → ~64).
    # Embed ~50/s: batchEmbedContents batch 5 × parallel 20, max-edge 1024
    # (≈2s/batch → 100 images / 2s = 50/s). Throughput is these semaphores —
    # NOT more Gunicorn workers.
    image_caption_batch_parallel: int = 8  # concurrent Gemini describe batches (semaphore)
    image_embed_backfill_parallel: int = 20  # concurrent batchEmbedContents workers (~50/s)
    image_embed_batch_size: int = 5         # images per batchEmbedContents call
    image_embed_max_edge: int = 1024        # longest edge before embed (0 = no downscale)
    # Cap work per maintenance tick; keep >= caption/embed parallel or you starve RPS.
    maintenance_batches_per_tick: int = 64

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
    # Cross-resolution image dedupe via OpenCV dHash (CPU-only, no extra deps).
    visual_dedupe_enabled: bool = True
    visual_dedupe_max_hamming: int = 5

    # Follow Google Drive folder shortcuts when listing/syncing the connected tree.
    follow_shortcut_folders: bool = True
    # Experimental Library overlay to manually name individual faces. Off by default.
    experimental_manual_face_tag: bool = False

    # TIFF/RAW decode: max attempts before permanent skip (stops infinite requeue loops).
    decode_max_attempts: int = 1

    webhook_secret: str = ""
    # Secret-gated production diagnostics. Disabled by default; POST /tests
    # runs a fixed DB-free suite plus read-only live checks.
    production_tests_enabled: bool = False
    production_tests_token: str = ""
    production_tests_timeout_seconds: int = 90
    production_tests_cooldown_seconds: int = 30

    # Google Drive push notifications → in-memory file-list cache.
    # Set DRIVE_WEBHOOK_URL to the public HTTPS endpoint Google will POST to
    # (e.g. https://dfi-backend-production.up.railway.app/api/webhooks/drive).
    # If empty, PUBLIC_BASE_URL + /api/webhooks/drive is used when HTTPS.
    drive_webhook_url: str = ""
    drive_webhook_channel_token: str = ""
    # Dev-only: accept simulated push POSTs without a matching channel id.
    # pydantic-settings parses "true"/"1"/"yes" as True.
    drive_webhook_allow_unverified: bool = False
    # Rare fallback full sync when push is active (seconds). Default 6h.
    drive_cache_fallback_sync_seconds: float = 21600.0

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

    # yt-dlp cookies for YouTube downloads on servers (Railway). Prefer a volume
    # path (YTDLP_COOKIES_FILE / YOUTUBE_COOKIES_FILE) or paste Netscape cookie
    # contents into YTDLP_COOKIES / YOUTUBE_COOKIES (written to a temp file at runtime).
    ytdlp_cookies_file: str = ""
    youtube_cookies_file: str = ""
    ytdlp_cookies: str = ""
    youtube_cookies: str = ""

    # Video indexing (ffmpeg frames + VTT transcript + Gemini VLM)
    video_indexing_enabled: bool = True
    video_cache_dir: str = "./data/videos"
    # Durable media copies for Drive images/docs (temp → atomic move; stable paths).
    media_cache_dir: str = "./data/media_cache"
    video_frame_interval_seconds: float = 1.0
    video_max_sample_frames: int = 300
    video_max_gemini_frames: int = 12
    video_vlm_enrich: bool = True
    # Max videos at once. InsightFace/CPU lock → diminishing returns above ~3–4;
    # raise via VIDEO_INDEX_MAX_PARALLEL only if ffmpeg/Gemini headroom shows idle.
    video_index_max_parallel: int = 3
    # Local ASR fallback when YouTube/Drive captions are missing (carousel needs text).
    whisper_model_size: str = "base"
    whisper_fallback_enabled: bool = True

    # Gemini API client-side concurrency (tune to your tier; see ai.google.dev rate limits).
    # Embedding 2: allow ~20 concurrent batchEmbedContents (batch=5 → ~50 img/s).
    # VLM: must be >= image_caption_batch_parallel or the caption semaphore is useless.
    gemini_embed_max_concurrent: int = 32
    gemini_vlm_max_concurrent: int = 16
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
    # Local transcript RAG (text→text) — replaces Gemini File Search for video speech.
    qdrant_video_transcripts_collection: str = "dfi_video_transcripts"
    gemini_video_result_limit: int = 30   # Qdrant candidates before re-rank
    gemini_video_min_score: float = 0.25
    gemini_video_display_min_score: float = 0.32   # cosine threshold — lower = more recall
    gemini_transcript_min_score: float = 0.35
    # Max ANN neighbors to pull from Qdrant for Gemini-embedding image search.
    # UI lazy-loads the returned list; do not use this as a display page size.
    gemini_image_result_limit: int = 10000
    gemini_image_min_score: float = 0.25

    # Query expansion (LLM rewrites → multi-vector fusion) for higher recall
    search_query_expansion: bool = True

    # Image captioning: index-time VLM description → caption text embedding.
    # Search compares the query against captions (text→text, well-calibrated),
    # which filters vague visual matches without slowing search.
    image_caption_enabled: bool = True
    image_caption_model: str = "gemini-3.5-flash-lite"
    image_caption_max_dim: int = 512          # downscale longest side before VLM
    image_caption_batch_size: int = 10        # images per Gemini describe call
    image_caption_min_words: int = 4          # captions shorter than this are re-generated
    qdrant_image_captions_collection: str = "dfi_image_captions"
    # Fusion of visual (image-embedding) and caption (text-embedding) cosine.
    image_visual_weight: float = 0.4
    image_caption_weight: float = 0.6
    image_caption_min_score: float = 0.32     # recall gate; reranking handles precision
    image_visual_strong_score: float = 0.50   # keep on strong visual alone

    # Durable backups (Postgres + Qdrant + carousel/deep-dive forever archives).
    backup_enabled: bool = True
    backup_dir: str = "./data/backups"
    # Keep all on-volume backup archives for three days; durable history belongs
    # in managed Postgres/Qdrant snapshots rather than an ever-growing app volume.
    backup_retention_days: int = 3
    backup_interval_seconds: int = 86400
    # Async SQLAlchemy pool — must fit under Postgres max_connections (200).
    # Budget: WEB_CONCURRENCY × (pool_size + max_overflow) << max_connections.
    # Prefer small pools + short-lived sessions over fat pools. UI poll storms and
    # indexer jobs share the same workers; fat pools (15+9)×4 ≈ 96 look "safe" on
    # paper but leave little room once drive-connector / pg_dump / admin connect.
    # Default 5+5 with WEB_CONCURRENCY=4 → 40 reserved (reuse, don't hoard).
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_timeout: float = 30.0
    # Qdrant REST client timeout (seconds); raise to avoid false connection errors under load.
    qdrant_timeout_seconds: float = 60.0

    # Append-only body/clothing re-id layer (body_signatures table).
    reid_enabled: bool = True
    reid_min_face_area_fraction: float = 0.015   # face must be prominent in frame
    reid_min_body_coverage: float = 0.55         # ≥55% of expected full-body extent visible
    reid_body_match_threshold: float = 0.70      # body cosine alone is never enough for identity
    # Head/face gate: same gown must NOT propose "Likely {person}" without ArcFace agreement.
    reid_face_gate_threshold: float = 0.45
    reid_candidate_face_weight: float = 0.75
    reid_candidate_body_weight: float = 0.25
    reid_backfill_max_parallel: int = 4

    # Append-only reverse-image / people-web identification (face_web_matches).
    # Preferred free path: Cohesivity Exa people search (image → Gemini clues → Exa).
    # Optional paid fallback: SERPAPI_KEY for true Google Lens reverse image search.
    # Free Google reverse image (SOME-1HING style). Hosted API first, scrape fallback.
    google_reverse_api_url: str = "https://google-reverse-image-api.vercel.app/reverse"
    # Official Google Cloud Vision Web Detection reverse-image API.
    # Enable Cloud Vision API + billing on the key's Google Cloud project.
    google_vision_api_key: str = ""
    # Apify Google Lens (preferred for identity — AI Mode / exact matches).
    # Token from https://console.apify.com/account/integrations
    apify_token: str = ""
    apify_google_lens_actor: str = "borderline/google-lens"
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
