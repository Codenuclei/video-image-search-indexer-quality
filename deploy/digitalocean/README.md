# DigitalOcean — Carousel Studio only

Deploy **only** Carousel Studio from branch `pruned-craousel`:

| Layer | Where | Source |
|---|---|---|
| `carousel-frontend` | App Platform (`.do/frontend.yaml`) | `carousel-frontend/` |
| `carousel-backend` | `dfi-carousel-data` Droplet compose | immutable DOCR `backend/` image |
| PostgreSQL 17 + pgvector, Qdrant | same Droplet compose | named volumes |
| HTTPS | Caddy on the Droplet | `Caddyfile` |
| Whisper + ArcFace | RunPod Serverless | `deploy/runpod-whisper/` |

The backend moved off App Platform because its ephemeral filesystem cannot hold
30–50GB Drive videos. Its durable cache is
`/opt/dfi-carousel/backend-data` on the 160GB Droplet. Search services remain
separate and must not use this stack.

## Architecture

```
Internet
   │
   ├─ App Platform dfi-carousel-frontend (:3002)
   │         API_PROXY_TARGET = https://api-carousel.139-59-35-242.sslip.io
   │
   └─ api-carousel.139-59-35-242.sslip.io
             │ Caddy :443
             └─ Droplet backend :8000
                    ├─ Postgres :5432 (Docker network)
                    ├─ Qdrant   :6333 (Docker network)
                    ├─ /opt/dfi-carousel/backend-data
                    └─ RunPod (Whisper / ArcFace) over HTTPS
```

## 1. Droplet stack

The production Droplet is `dfi-carousel-data` in BLR1. Copy this directory to
`/opt/dfi-carousel`, create `.env`, and start the stack:

```bash
cd /opt/dfi-carousel
cp .env.example .env
chmod 600 .env
# Fill Postgres, Google, AI, and RunPod secrets.
# Set CAROUSEL_BACKEND_IMAGE to an immutable DOCR tag.
docker compose --env-file .env up -d
docker compose ps
```

Deploy a new backend image after refreshing `/root/.docker/config.json` with a
read/write DOCR login:

```bash
./scripts/deploy-backend.sh <git-sha>
```

The sslip.io hostname resolves directly to `139.59.35.242`; no managed DNS
account is required. The firewall allows public TCP 80/443, restricted admin
SSH, and keeps 5432/6333 private.

## 2. Build and publish

```bash
# From repo root on branch pruned-craousel
export IMAGE_TAG="$(git rev-parse --short HEAD)"
doctl registry login --expiry-seconds 3600
docker buildx build --platform linux/amd64 \
  -t "registry.digitalocean.com/mu-pitch-studio/dfi-carousel-backend:${IMAGE_TAG}" \
  --push backend

docker buildx build --platform linux/amd64 \
  --build-arg API_PROXY_TARGET=https://api-carousel.139-59-35-242.sslip.io \
  --build-arg NEXT_PUBLIC_API_URL=/api/proxy \
  --build-arg NEXT_PUBLIC_BACKEND_URL=https://api-carousel.139-59-35-242.sslip.io \
  -t "registry.digitalocean.com/mu-pitch-studio/dfi-carousel-frontend:${IMAGE_TAG}" \
  --push carousel-frontend

doctl apps spec validate .do/frontend.yaml
```

On Apple Silicon, build amd64 images on the amd64 Droplet if QEMU is unstable.

### OAuth

Until the raw URL can be added to Google Cloud, keep the already-authorized App
Platform callback as a lightweight OAuth relay:

`https://dfi-carousel-backend-zf4xh.ondigitalocean.app/auth/google/callback`

API key HTTP referrer:

`https://dfi-carousel-frontend-e9k4e.ondigitalocean.app/*`

## 3. RunPod

See [`deploy/runpod-whisper/README.md`](../runpod-whisper/README.md). Set in the
Droplet backend `.env` only:

- `RUNPOD_API_KEY`
- `RUNPOD_WHISPER_ENABLED=true` + `RUNPOD_WHISPER_ENDPOINT_ID`
- `RUNPOD_FACE_GPU_ENABLED=true` + `RUNPOD_FACE_ENDPOINT_ID`

## 4. Backup / restore

Collections snapshotted by `scripts/backup.sh`:

- `dfi_video_frames`
- `dfi_images`
- `dfi_image_captions`
- `dfi_video_transcripts`

```bash
# On Droplet
./scripts/backup.sh
# Optional: set SPACES_* + BACKUP_ENCRYPTION_PASSPHRASE for encrypted Spaces upload

./scripts/restore.sh /var/backups/dfi-carousel/<stamp>
# or .tar.gz / .tar.gz.enc
```

Schedule via cron (`0 3 * * *`) after the first successful manual run.

## 5. Preflight (local, no cloud changes)

```bash
chmod +x deploy/digitalocean/scripts/*.sh
./deploy/digitalocean/scripts/preflight.sh
backend/.venv/bin/python -m pytest deploy/digitalocean/tests/test_artifacts.py -q
```

## 6. Frontend proxy request limit

The frontend's App Platform proxy hard-caps HTTP requests at **100 seconds**.
Studio long work must not depend on holding one HTTP socket. Backend job and
video state now lives durably on the Droplet.

### Durable job map (`async-jobs`)

| Studio step | Mechanism | Initiate | Poll |
|---|---|---|---|
| Transcript ensure | DriveFile phase markers + `BackgroundTasks` | `POST .../videos/ensure-transcript` | `GET .../videos/{id}/transcript-status` |
| Themes | `CarouselGenerationSave` (`status=processing\|ready\|error`) | `POST .../pipeline/themes/jobs` | `GET .../pipeline/themes/jobs/{id}` |
| Extract / extract hooks | `carousel_studio_jobs` (kind `extract` / `extract_hooks`) | `POST .../pipeline/extract` or `.../extract/hooks` (returns within ~75s; may be `status=running`) | `GET .../pipeline/extract/status?job_id=` / `.../extract/hooks/status?job_id=` |
| Generate | `carousel_studio_jobs` (kind `generate`) | `POST .../pipeline/generate` (≤~75s or `status=running`) | `GET .../pipeline/generate/status?job_id=` |
| Select images / visual prep | `carousel_studio_jobs` (kind `visual_prep`) | `POST .../pipeline/select-images` (≤~75s or `status=preparing`) | `GET .../pipeline/select-images/status?drive_file_id=&job_id=` |

Interactive HTTP budget is `_STUDIO_HTTP_BUDGET_SEC` / `_SELECT_IMAGES_REQUEST_TIMEOUT_SEC` (75s) so responses return before the 100s frontend proxy kill. `/test/studio` polls via `carousel-frontend/lib/test-api.ts` (`themes`, `extract`, `extractHooks`, `generate`, `pollSelectImages`). Keep `WEB_CONCURRENCY=1` on the Droplet backend.

**Remaining timeout risks:** sync `POST /pipeline/themes` (prefer `/themes/jobs`), `POST /pipeline/prerun` (multi-video warm), and any client that ignores `status=running|preparing` and does not poll. Background workers are in-process (`asyncio.create_task` / `BackgroundTasks`); status rows survive restart and status polls re-kick idle jobs when request bodies were stored.

## Rollback

After validation, replace the previous App Platform backend workload with the
small OAuth redirect relay required by the pre-authorized Google callback. All
API and indexing work remains on the Droplet. Do not deploy or modify Railway.

## Out of scope for these artifacts

- Creating new Droplets, VPCs, Spaces, or App Platform apps
- Committing secrets
