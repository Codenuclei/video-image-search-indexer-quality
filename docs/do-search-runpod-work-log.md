# DigitalOcean search + RunPod buffalo_l — work log

Session recap (through 9 Sep 2026). **Never `railway up`.** Railway `drivefaceindexer` stays serving as rollback. Work is DigitalOcean search plus RunPod **serverless** faces only (never dedicated pods). Branch: `do-search-runpod`.

## Locked constraints

- Search copied to DigitalOcean. GPU is RunPod serverless only.
- Faces: InsightFace `buffalo_l`, same process as CPU `FaceEngine.detect_faces` (`FaceAnalysis.get`, det_size 640, conf 0.5, 512-d, full-float).
- API box: Drive download + Postgres persist. Bytes in; **never Drive URLs** to RunPod.
- Video: 10 GB cap, HMAC-signed HTTPS pull + parallel Range GET (not JSON/base64).
- DO search-api droplet: **no InsightFace** on the 8 GB box when GPU is configured.
- Serverless scale: load `{workersMin:1, workersMax:1}`; idle `{0,0}`.
- Do **not** set exclusive `RUN_FACE_WORKER=true` (that kills `IdentifyWorkerLoop`).
- Do not put an empty `RUNPOD_API_KEY=` in compose `environment:` (it wipes `backend.env`).

## Live hosts

| What | Where |
|---|---|
| UI | https://165-245-170-117.sslip.io |
| API | https://api.165.245.170.117.sslip.io |
| Droplet | `root@165.245.170.117` (SSH `:22` is flaky; retries work). Key `~/.ssh/id_ed25519` |
| Face endpoint | `0zub88paibpsf3` (`dfi-face-buffalo`) |
| Template | `z0jpvc1ebf` → `ghcr.io/codenuclei/video-image-search-indexer-quality/dfi-face-buffalo:gpu` |
| Compose | `/opt/dfi/video-image-search-indexer-quality/deploy/search` |

## What shipped

### 1. Search-only DigitalOcean stack

- One search-api droplet + Docker Compose: Caddy, frontend, backend, Drive connector, Qdrant on the box / VPC.
- Postgres restored from Railway (`pgvector`). Qdrant collections restored from snapshots (`dfi_images`, `dfi_image_captions`, `dfi_video_frames`, plus transcripts / folder contexts as copied).
- Frontend baked with `NEXT_PUBLIC_API_URL=https://api.165.245.170.117.sslip.io`.
- Indexer and auto-index left **off** (`RUN_INDEXER=false`, `AUTO_INDEX_ENABLED=false`).
- Copy scripts: `deploy/search/data-copy.sh`, `deploy/search/data-copy-execute.sh`. Railway was not dropped.

Approximate restored index: ~14,313 images, ~14,294 captions, ~161,109 video frames.

### 2. Gemini keys + poisoned search cache

After Gemini credits came back:

- Deleted the empty `coffee` row in `search_query_cache` (poisoned while billing was exhausted).
- `/search?q=coffee&rerank=false` → **71 files**.
- Other checks: hyrox delhi, ceremonial cheque, testv2 coffee still returned results.
- Later confirm: Gemini `batchEmbedContents` 200; coffee still 71 files after backend recreate.

### 3. RunPod buffalo_l serverless (code)

Handler wraps the CPU path on CUDA:

- `runpod/face-buffalo/handler.py` — `FaceAnalysis.get`, JPEG `images[]` / `image_b64`, signed `video_url` + timestamps, NVDEC ffmpeg then CPU ffmpeg, always delete `/tmp`.
- `backend/app/faces/runpod_gpu.py` — JPEG bytes in, never Drive URLs; PATCH workers 1/1 under load, 0/0 idle.
- `backend/app/faces/video_pull.py` + `routers/face_gpu_pull.py` — HMAC-signed HTTPS pull.
- Image/video pipelines call RunPod when GPU is configured; no local InsightFace on the droplet in that mode.
- Create script: `scripts/runpod_create_face_endpoint.py` (`REQUEST_COUNT` scaler, rest min/max 0).

### 4. `nvidia-smi` crash (this session)

First still JPEG (`60aa0cc2-…`) **FAILED**:

```
ValueError: could not convert string to float: '[Insufficient Permissions]'
```

RunPod serverless returns that string for VRAM fields. `_nvidia_smi` now catches `ValueError` and records it as telemetry instead of crashing. Test: `backend/tests/test_face_buffalo_handler.py`.

Fix was SCP’d to the droplet, image rebuilt (COPY `handler.py` not cached), pushed:

`ghcr.io/codenuclei/video-image-search-indexer-quality/dfi-face-buffalo:gpu`  
digest `sha256:4c32629c900f4624ebb11fb704cacaa8c6d2252cbe836153ede8e5fb0e1b4ba8`

GHCR login used a piped `gh auth token` on the droplet, then `docker logout` and `/root/.docker/config.json` removed. Do not store GitHub OAuth as RunPod registry auth.

Scaler was patched to `REQUEST_COUNT` / 1 (was `QUEUE_DELAY` / 4). Endpoint timeout 1h.

`rest.runpod.io` from Python `urllib` can 403 Cloudflare 1010; curl / a browser User-Agent works. `api.runpod.ai/v2` job APIs were fine.

### 5. Smoke after the new image

Workers recycled 0→1, then:

| Job | Result |
|---|---|
| Healthcheck `1e71292d-…` | `ok: true`, `CUDAExecutionProvider`, ffmpeg `cuda` in hwaccels. VRAM fields still `[Insufficient Permissions]` (ignored). |
| Still JPEG `3c14b4de-…` (`runpod/qwen-vl/prod-hits/043_0H1A8472.jpg`) | 1 face, conf ~0.81, **512-d** float embedding, thumbnail present. First inference ~85s (cold model load). |

Workers PATCHed back to **min 0 / max 0**. `workersStandby` is 1 and is **not** in the REST PATCH schema; idle timeout 120s.

### 6. Face jobs on the API box (no local InsightFace)

`RUNPOD_API_KEY` was already in droplet `backend.env` but **not** in the running container until recreate.

Compose + `backend.env`: `FACE_JOBS_ENABLED=true`. Recreated **backend only**.

Running flags:

- `FACE_JOBS_ENABLED=true`
- `RUN_FACE_WORKER=false`
- `RUN_INDEXER=false`
- `RUNPOD_FACE_GPU_ENABLED=true`
- `RUNPOD_API_KEY` set (len 50)

Boot:

- Gunicorn worker 1: `Identify Qwen worker loop started` + `Face worker loop started on API leader (RunPod)`
- Other worker: API-only (not leader)
- Indexer loops skipped

Copied DB face/identify/object queues (so FaceWorkerLoop would not drain a huge backlog):

| Table | Status |
|---|---|
| `face_jobs` | DONE 11780, ERROR 389, **0 pending** |
| `identify_jobs` | DONE 15215, ERROR 3 |
| `object_jobs` | DONE 22948, ERROR 2 |
| `ocr_jobs` | PENDING 69, DONE 34 (`OcrWorkerLoop` is **not** started from `main.py`) |

Search after recreate: `/health` 200, coffee still 71 files.

### 7. Git / identity (earlier in the same convo)

- Feature work on `do-search-runpod`.
- Author rewrite: Brand MU / `mu-mac_3@Brands-Mac-mini.lan` → `Codenuclei <abhishekghosh.air1@gmail.com>` on `main` and `do-search-runpod` (force-push after explicit confirmation). Trees unchanged. Sudeep left as Sudeep.
- GitHub contribution graph only counts default branch `main`.
- Other remotes still have Brand MU (`origin/pruned-craousel`, `origin/codenuclei/file-count-and-shell-chrome`, local `cursor/selected-push`).
- Droplet git was still `do-search-runpod` at `092748f` (older than rewritten remote tips). Handler.py on the droplet was patched in the worktree for the image build.

Local git config was **not** changed; author set via env / filter-branch.

## Current droplet compose intent

```yaml
RUN_INDEXER: "false"
RUN_FACE_WORKER: "false"
FACE_JOBS_ENABLED: "true"
RUNPOD_FACE_GPU_ENABLED: "true"
RUNPOD_FACE_ENDPOINT_ID: 0zub88paibpsf3
RUNPOD_FACE_MAX_EDGE: "0"
RUNPOD_FACE_VIDEO_MAX_BYTES: "10737418240"
```

API leader FaceWorkerLoop calls RunPod when configured. Do not set exclusive `RUN_FACE_WORKER=true`.

## Not done / leftover

- Video GPU download smoke still needs a **cached** file on the API box (HMAC 403/404 already pass against `PUBLIC_BASE_URL`). Indexer is off, so `/internal/face-gpu-video/{id}` 404s for cache misses.
- OCR loop is wired but **idle** (`ocr_lane_enabled` default false). 69 pending `ocr_jobs` drain only if that flag is turned on — do not enable for search-only.
- Sync droplet git past `092748f` and recreate backend so Range pull + OCR loop are live on the box.
- Turn indexer back on (only when ingest is wanted).
- `workersStandby: 1` cannot be PATCHed via REST body; GPU idle is min/max 0 + 120s idle timeout.

## Commands that matter (no Railway)

```bash
# Search
curl -sS 'https://api.165.245.170.117.sslip.io/health'
curl -sS -G 'https://api.165.245.170.117.sslip.io/search' --data-urlencode 'q=coffee' --data-urlencode 'rerank=false'

# Recreate backend only (picks up backend.env; do not pass empty RUNPOD_API_KEY)
cd /opt/dfi/video-image-search-indexer-quality/deploy/search
docker compose --env-file .env up -d --force-recreate --no-deps backend

# Buffalo image (on droplet after SCP of handler.py)
docker build -t ghcr.io/codenuclei/video-image-search-indexer-quality/dfi-face-buffalo:gpu \
  /opt/dfi/video-image-search-indexer-quality/runpod/face-buffalo
# login with piped token, push :gpu, logout, rm /root/.docker/config.json
```

REST scale (browser User-Agent if Cloudflare 1010):

- Load: `PATCH /v1/endpoints/0zub88paibpsf3` `{"workersMin":1,"workersMax":1}`
- Idle: `{"workersMin":0,"workersMax":0}`

## 10 Sep 2026 — migration implementation parity release

Release commit: `39a736314cd5af50e011c0ee8472e9211eacc65f`

- Added additive `qwen_enrichment_jobs`, `qwen_captions`, and
  `video_segment_labels` schema plus Alembic revision `0002_qwen_enrichment`.
  Qwen raw output, normalized object/action labels, and exactly one canonical
  caption per target/prompt/model version are durable. Canonical caption text
  is embedded through Gemini Embedding 2; Gemini does not generate the caption.
- Qwen image requests use the RunPod `images[]` batch contract. Video Qwen
  evidence is attached to the exact `VideoSegment`; `/search/testv2` reads the
  timestamped evidence while production `/search` remains on its existing path.
- Removed all new `ObjectWorkerLoop` startup and `enqueue_object_job` scheduling.
  Historical object tables, rows, and worker implementation remain untouched.
- RunPod video processing is now one bounded job per video: one signed source
  pull, NVDEC extraction, buffalo_l faces, and returned JPEG evidence. The
  client and handler enforce 80-frame and 48 MiB JPEG-response caps. A configured
  RunPod failure is retryable and never falls back to host InsightFace.
- New YouTube rows are excluded from video claims. External VTT remains
  supported; DO keeps `WHISPER_FALLBACK_ENABLED=false`.
- DO enables `GEMINI_GENERATION_DISABLED=true`, disables Gemini caption
  backfill/filter/query expansion, and retains `models/gemini-embedding-2`.

Focused local verification: `79 passed, 3 skipped`. A broader selection reached
`81 passed, 3 skipped`; its three errors were only database fixtures failing to
connect to the absent local PostgreSQL test port `55432`.

DigitalOcean parity evidence:

- Local, GitHub, droplet checkout, and `/version` all report
  `39a736314cd5af50e011c0ee8472e9211eacc65f`.
- Immutable backend image:
  `sha256:1543d33a2b6d2bfd51f7eba3c09ef133d8e953a24216a77652742883af9a3015`.
- Face worker image:
  `sha256:b1ee5515acec86c57c27a3d69ef81c98b0f73a2819def5fd7eb4e02a6a55de70`,
  and RunPod template `z0jpvc1ebf` is pinned to the release-SHA tag.
- Backend mounts only the named Docker volume at `/app/data`; no host bind
  mounts. `RUN_INDEXER=false`, `AUTO_INDEX_ENABLED=false`, and all identify,
  object, and backfill runtime lane flags are false pending cutover approval.
- `/health` is 200; `/search?q=coffee&rerank=false` remains 71 files.
- Durable schema is live. Existing image migration created 146 versioned Qwen
  jobs and 146 canonical captions before lanes were paused; no video segment
  labels exist yet because no new video indexing canary has been authorized.

Readiness: mission-critical migration code is present on DO. Engineering is
approximately 96% complete. Approval/cutover validation is approximately 75%
complete: start with one image and one video canary when approved, specifically
validating the changed one-job video response/failure caps and terminal source
cleanup before enabling general indexing. Carousel migration remains deferred.
