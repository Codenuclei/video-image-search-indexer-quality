# Dedicated indexer + InsightFace runner split (same Docker image)

Use the **same Docker image** as `dfi-backend`.

## API service (`dfi-backend`)

```
RUN_INDEXER=false
RUN_FACE_WORKER=false
FACE_JOBS_ENABLED=false
WEB_CONCURRENCY=2   # or 4 for search traffic
```

Search, settings, Drive OAuth, and `POST /index` (listing sync only) stay here so the UI stays responsive under index load.

**Replicas: 1** while a volume is attached (Railway: volumes cannot use replicas).

## Indexer service (`dfi-indexer`)

```
RUN_INDEXER=true
RUN_FACE_WORKER=false
FACE_JOBS_ENABLED=true
WEB_CONCURRENCY=1
GUNICORN_WORKERS=1
IMAGE_INDEX_MAX_PARALLEL=40
DRIVE_DOWNLOAD_MAX_CONCURRENT=40
IMAGE_EMBED_BACKFILL_PARALLEL=10
IMAGE_EMBED_BATCH_SIZE=5
INDEX_STATUS_BATCH_SIZE=100
INDEX_DB_MAX_CONCURRENT=24
DB_POOL_SIZE=30
DB_MAX_OVERFLOW=20
INDEX_DISK_HIGH_WATER_BYTES=2147483648
```

Replicas: **1** (volume + single `IndexStatusBatcher` owner). Mount the same `/app/data` volume.

With `FACE_JOBS_ENABLED=true`, image prepare (cache/hash/Media) runs here and enqueues `face_jobs`; InsightFace does **not** run inline. Video frame/transcript indexing also runs here; per-frame InsightFace is deferred to the face fleet when `FACE_JOBS_ENABLED=true`.

## Face worker fleet (`dfi-face-worker`)

Volume-less InsightFace runners (sequential per replica; limited RAM):

```
RUN_INDEXER=false
RUN_FACE_WORKER=true
FACE_JOBS_ENABLED=true
WEB_CONCURRENCY=1
GUNICORN_WORKERS=1
FACE_WORKER_CONCURRENCY=1
FACE_JOB_LEASE_SECONDS=900
FACE_JOB_MAX_ATTEMPTS=3
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=5
IMAGE_INDEX_MAX_PARALLEL=1
```

| Item | Value |
|---|---|
| v1 replicas | `railway scale -s dfi-face-worker sfo=4` |
| Ceiling | 8 |
| Per replica | ~2 GB RAM / 2 vCPU (do not fatten — InsightFace is sequential) |
| Volume | **none** (required for horizontal replicas) |

Each runner claims jobs with `FOR UPDATE SKIP LOCKED`, runs buffalo_l detect, then pushes the embed/status path.

Shared: same Postgres, Qdrant, Drive OAuth secrets. Face workers re-fetch image bytes from Drive onto ephemeral disk (no shared volume).

## Behaviour

- Indexer prepare → `face_jobs` → face fleet → embed queue (batch 5 × ≤10) → status flush every 100 + **Drive cache unlink** on PROCESSED/ERROR.
- Claim gates pause when free disk < `INDEX_DISK_HIGH_WATER_BYTES`.
- Permanent library: sync never demotes or archives `PROCESSED` rows; indexing is add-if-missing.
- Until split exists, keep `RUN_INDEXER=true` and `FACE_JOBS_ENABLED=false` on the single backend (inline faces).

## Production cutover checklist

1. Deploy code with face_jobs schema + unlink + error-bucket retry.
2. Create `dfi-indexer` (volume, replicas=1) and set knobs above; set `RUN_INDEXER=false` on `dfi-backend`.
3. Create `dfi-face-worker` (**no volume**); set face-worker env; `railway scale -s dfi-face-worker sfo=4`.
4. Enable `FACE_JOBS_ENABLED=true` on indexer only after face workers are healthy.
5. Verify logs: `face_job_enqueued`, `face_job_done`, `index_status_batch_unlink`, `EmbedQueue batch upserted=`.
6. Selective ERROR retry: `GET /index/error-stats` then `POST /index/errors/retry` with bucket (never junk/dupe). Confirm disk headroom before requeueing `enospc`.
