# Drive push notifications (Option 1) → file-list cache + DB sync

Google Drive can push change notifications to the backend so we stop continuous
Drive tree walks that starved a single uvicorn worker. The frontend keeps polling
existing routes (`/drive/files`, `/drive/library`, …); `GET /api/cache/files`
exposes the Drive listing for fast reads / ops.

Production is intended to run **Gunicorn + UvicornWorker** (`WEB_CONCURRENCY=24`
on 24 vCPU). In-memory cache and push-channel state are **per-process**; the
pragmatic model is:

1. Webhook hits any worker → that worker refreshes Drive listing and **syncs Postgres**
2. `GET /api/cache/files` / status **prefer DB** so every worker sees the same list
3. Exactly one worker is elected (Postgres advisory lock) as **background leader**
   for indexer / maintenance / `changes.watch` registration
4. Carousel extract/theme storms use advisory locks across workers

Docs: https://developers.google.com/workspace/drive/api/guides/push

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/cache/files` | Drive file list (DB-first; memory fallback) |
| `GET` | `/api/cache/status` | Cache + push-channel health (no file payload) |
| `POST` | `/api/webhooks/drive` | Google Drive push receiver (also `/webhooks/drive`) |
| `POST` | `/webhooks/drive-changed` | Legacy Drive Connector webhook (`X-Webhook-Secret`) |
| `GET` | `/drive/files` | Unchanged — DB-backed indexed file list (frontend poll) |

Webhook ACK: returns `204` quickly; cache refresh + DB sync run in a background task.

## Env vars

| Variable | Required | Description |
|----------|----------|-------------|
| `DRIVE_WEBHOOK_URL` | Prefer for prod | Full HTTPS URL Google POSTs to, e.g. `https://dfi-backend-production.up.railway.app/api/webhooks/drive` |
| `PUBLIC_BASE_URL` | Alt | If HTTPS and `DRIVE_WEBHOOK_URL` empty, webhook address = `{PUBLIC_BASE_URL}/api/webhooks/drive` |
| `DRIVE_WEBHOOK_CHANNEL_TOKEN` | **Required with multi-worker** | Opaque token echoed as `X-Goog-Channel-Token`; must be shared so any worker can verify pushes |
| `WEBHOOK_SECRET` | For connector | Existing secret for `/webhooks/drive-changed`; also fallback push token |
| `DRIVE_WEBHOOK_ALLOW_UNVERIFIED` | Dev only | `true` to accept simulated push POSTs without a registered channel (never on Railway) |
| `DRIVE_CACHE_FALLBACK_SYNC_SECONDS` | Optional | Rare full sync when push is active (default `21600` = 6h) |
| `WEB_CONCURRENCY` / `GUNICORN_WORKERS` | Prod | Worker count (default `24` for 24 vCPU). Local: leave unset and use uvicorn. |

## Railway (`dfi-backend`) — deploy only when explicitly approved

1. Set:
   ```
   DRIVE_WEBHOOK_URL=https://dfi-backend-production.up.railway.app/api/webhooks/drive
   DRIVE_WEBHOOK_CHANNEL_TOKEN=<long-random-string>
   DRIVE_WEBHOOK_ALLOW_UNVERIFIED=false
   WEB_CONCURRENCY=24
   ```
2. Deploy only after an explicit ask. Dockerfile starts `scripts/gunicorn_start.sh`
   (Gunicorn + UvicornWorker). Logs should show many `Booting worker` lines and one
   `this worker is the background leader`.
3. Confirm: `GET /health` → 200; `GET /api/cache/status` shows `from_db=true` / warm
   listing and push config.

Google requires a valid public SSL certificate (Railway provides this).

**Do not** recreate volumes or wipe Postgres when changing worker count.

## Local

### Dev (unchanged)

```bash
cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000
```

Without a public HTTPS URL, the backend still:

1. Seeds the in-memory cache on the background leader with one Drive list (+ DB sync)
2. Serves `GET /api/cache/files` from DB when rows exist, else memory
3. Implements the webhook path fully for when a tunnel URL is set

### Optional multi-worker smoke

```bash
cd backend
WEB_CONCURRENCY=4 GUNICORN_TIMEOUT=900 ./scripts/gunicorn_start.sh
```

### Register a real push channel locally

1. Start a tunnel to the backend, e.g.:
   ```bash
   cloudflared tunnel --url http://127.0.0.1:8000
   ```
2. Put the HTTPS origin into env (or rely on `PUBLIC_BASE_URL` from `scripts/sync-tunnel-env.sh`):
   ```bash
   # backend/.env
   DRIVE_WEBHOOK_URL=https://<your-tunnel>.trycloudflare.com/api/webhooks/drive
   DRIVE_WEBHOOK_CHANNEL_TOKEN=local-dev-token
   DRIVE_WEBHOOK_ALLOW_UNVERIFIED=false
   ```
3. Restart uvicorn (or gunicorn). Logs should show `Drive push channel registered`.
4. Simulate (or wait for a real Drive change):
   ```bash
   curl -i -X POST http://127.0.0.1:8000/api/webhooks/drive \
     -H 'X-Goog-Channel-ID: any' \
     -H 'X-Goog-Channel-Token: local-dev-token' \
     -H 'X-Goog-Resource-State: sync' \
     -H 'X-Goog-Message-Number: 1' \
     -H 'X-Goog-Resource-ID: test'
   ```
   For unverified local smoke tests only, set `DRIVE_WEBHOOK_ALLOW_UNVERIFIED=true`.

## Behaviour summary

- **Primary updates:** Google push → async cache refresh + DB `sync_file_list` (advisory-locked across workers)
- **Startup (leader only):** seed list + DB sync; register `changes.watch`
- **API workers:** schema/settings boot only; serve traffic; no indexer loops
- **Fallback:** when auto-index is on, a rare full sync (15m without push, 6h with push) on the leader
- **Channel renew:** leader background loop re-calls `changes.watch` before expiry
