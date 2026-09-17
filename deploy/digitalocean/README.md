# DigitalOcean — Carousel Studio only

Deploy **only** Carousel Studio from branch `pruned-craousel`:

| Layer | Where | Source |
|---|---|---|
| `carousel-backend` | App Platform app (`.do/backend.yaml`) | `backend/` |
| `carousel-frontend` | App Platform app (`.do/frontend.yaml`) | `carousel-frontend/` |
| PostgreSQL 17 + pgvector, Qdrant | Droplet compose (`docker-compose.yml`) | same VPC as **both** apps |
| Whisper + ArcFace | RunPod Serverless | `deploy/runpod-whisper/` |

Two App Platform apps are required: a single app with two services both claiming path prefix `/` fails `doctl apps spec validate`. Each app has exactly one service and owns `/` on its own hostname.

Search services (`dfi-backend` / `dfi-frontend` / `dfi-face-worker`) and Railway Studio remain untouched. Do not point these specs at `main`.

## Architecture

```
Internet
   │
   ├─ App Platform app dfi-carousel-frontend (:3002)
   │         API_PROXY_TARGET = https://<backend-public>
   │
   └─ App Platform app dfi-carousel-backend (:8000)
              │ VPC (private) — same REPLACE_WITH_VPC_UUID on both apps
              ├─ Droplet Postgres :5432 (pgvector)
              └─ Droplet Qdrant   :6333
              │
              └─ RunPod (Whisper / ArcFace) over public HTTPS
```

## 1. Provision Droplet data plane (manual)

This repo does **not** create cloud resources. Suggested sequence:

```bash
# Create VPC + Droplet in the same region as the apps (e.g. nyc1 / nyc)
doctl vpcs create --name dfi-carousel-vpc --region nyc1
doctl compute droplet create dfi-carousel-data \
  --region nyc1 --size s-2vcpu-4gb --image docker-20-04 \
  --vpc-uuid <vpc-uuid> --wait

# Cloud Firewall: allow 5432/6333 only from the VPC CIDR (not 0.0.0.0/0).
# SSH from your admin IP only.
```

On the Droplet:

```bash
git clone -b pruned-craousel \
  https://github.com/Codenuclei/video-image-search-indexer-quality.git
cd video-image-search-indexer-quality/deploy/digitalocean
cp .env.example .env
chmod 600 .env
# Set POSTGRES_PASSWORD=$(openssl rand -base64 32)
# Set DROPLET_BIND_ADDR=<droplet private IP>  # required for App Platform VPC clients
docker compose --env-file .env up -d
docker compose ps
```

Confirm `pg_isready` and `curl -sf http://$DROPLET_BIND_ADDR:6333/readyz`.

## 2. App Platform (two apps)

```bash
# From repo root on branch pruned-craousel
# Validate locally (requires doctl auth):
doctl apps spec validate .do/backend.yaml
doctl apps spec validate .do/frontend.yaml

# Edit both specs before create:
#  - .do/backend.yaml: DATABASE_URL / QDRANT_URL → Droplet private IP + password
#  - .do/backend.yaml: SECRET REPLACE_ME values → real keys (or Encrypt in UI)
#  - .do/backend.yaml: CAROUSEL_FRONTEND_URL / ALLOWED_ORIGINS after frontend is live
#  - .do/frontend.yaml: API_PROXY_TARGET + NEXT_PUBLIC_BACKEND_URL → backend public https
#  - Both: uncomment vpc.id with the SAME VPC UUID (doctl vpcs list)
#
# VPC note: create apps first if needed, then enable VPC via control panel or:
#   doctl apps update <backend-app-id> --spec .do/backend.yaml
#   doctl apps update <frontend-app-id> --spec .do/frontend.yaml
# VPC and dedicated egress IPs cannot both be enabled.

doctl apps create --spec .do/backend.yaml
doctl apps create --spec .do/frontend.yaml
```

Env `type` enums must be uppercase `GENERAL` / `SECRET` (validated by doctl).

### Cross-app URLs (no PRIVATE_URL)

Bindables do **not** resolve across App Platform apps. After backend is live:

1. Set frontend `API_PROXY_TARGET` and `NEXT_PUBLIC_BACKEND_URL` to the backend public https origin (or custom API domain).
2. Redeploy the frontend app so the Docker build bakes `NEXT_PUBLIC_BACKEND_URL`.
3. Set backend `CAROUSEL_FRONTEND_URL` / `ALLOWED_ORIGINS` to the frontend public https origin.

There is no `${…PRIVATE_URL}` link between these two apps; the UI talks to the API over the public backend URL (same pattern as Railway).

### OAuth / domains

Register Google redirect URI:

`https://<carousel-backend-public>/auth/google/callback`

API key HTTP referrer:

`https://<carousel-frontend-public>/*`

Keep the Railway Studio redirect URI until cutover is confirmed.

## 3. RunPod

See [`deploy/runpod-whisper/README.md`](../runpod-whisper/README.md). Set on the **backend** app only:

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

## 6. App Platform request limit

App Platform hard-caps HTTP requests at **100 seconds** and container filesystems are **ephemeral**. Studio long work must not depend on holding one HTTP socket or on local disk job files.

### Durable job map (`async-jobs`)

| Studio step | Mechanism | Initiate | Poll |
|---|---|---|---|
| Transcript ensure | DriveFile phase markers + `BackgroundTasks` | `POST .../videos/ensure-transcript` | `GET .../videos/{id}/transcript-status` |
| Themes | `CarouselGenerationSave` (`status=processing\|ready\|error`) | `POST .../pipeline/themes/jobs` | `GET .../pipeline/themes/jobs/{id}` |
| Extract / extract hooks | `carousel_studio_jobs` (kind `extract` / `extract_hooks`) | `POST .../pipeline/extract` or `.../extract/hooks` (returns within ~75s; may be `status=running`) | `GET .../pipeline/extract/status?job_id=` / `.../extract/hooks/status?job_id=` |
| Generate | `carousel_studio_jobs` (kind `generate`) | `POST .../pipeline/generate` (≤~75s or `status=running`) | `GET .../pipeline/generate/status?job_id=` |
| Select images / visual prep | `carousel_studio_jobs` (kind `visual_prep`) | `POST .../pipeline/select-images` (≤~75s or `status=preparing`) | `GET .../pipeline/select-images/status?drive_file_id=&job_id=` |

Interactive HTTP budget is `_STUDIO_HTTP_BUDGET_SEC` / `_SELECT_IMAGES_REQUEST_TIMEOUT_SEC` (75s) so responses return before the 100s platform kill. `/test/studio` polls via `carousel-frontend/lib/test-api.ts` (`themes`, `extract`, `extractHooks`, `generate`, `pollSelectImages`). Keep `WEB_CONCURRENCY=1` on the backend app.

**Remaining timeout risks:** sync `POST /pipeline/themes` (prefer `/themes/jobs`), `POST /pipeline/prerun` (multi-video warm), and any client that ignores `status=running|preparing` and does not poll. Background workers are in-process (`asyncio.create_task` / `BackgroundTasks`); status rows survive restart and status polls re-kick idle jobs when request bodies were stored.

## Rollback

Leave Railway `dfi-carousel` / `dfi-carousel-backend` / `Postgres-WEBK` / `dfi-carousel-qdrant` running until DigitalOcean backups and production behavior are verified. DNS / OAuth cutover is a later stage.

## Out of scope for these artifacts

- Creating Droplets, VPCs, Spaces, or App Platform apps
- Migrating Railway data
- DNS cutover
- Committing secrets
