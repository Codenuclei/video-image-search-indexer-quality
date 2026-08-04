# Drive push notifications (Option 1) → in-memory file-list cache

Google Drive can push change notifications to the backend so we stop continuous
Drive tree walks that starved uvicorn. The frontend keeps polling existing
routes (`/drive/files`, `/drive/library`, …); `GET /api/cache/files` exposes the
in-memory Drive listing for fast reads / ops.

Docs: https://developers.google.com/workspace/drive/api/guides/push

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/cache/files` | In-memory Drive file list (no Drive API call) |
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
| `DRIVE_WEBHOOK_CHANNEL_TOKEN` | Recommended | Opaque token echoed as `X-Goog-Channel-Token`; falls back to `WEBHOOK_SECRET` then a process-local random |
| `WEBHOOK_SECRET` | For connector | Existing secret for `/webhooks/drive-changed` |
| `DRIVE_WEBHOOK_ALLOW_UNVERIFIED` | Dev only | `true` to accept simulated push POSTs without a registered channel (never on Railway) |
| `DRIVE_CACHE_FALLBACK_SYNC_SECONDS` | Optional | Rare full sync when push is active (default `21600` = 6h) |

## Railway (`dfi-backend`)

1. Set:
   ```
   DRIVE_WEBHOOK_URL=https://dfi-backend-production.up.railway.app/api/webhooks/drive
   DRIVE_WEBHOOK_CHANNEL_TOKEN=<long-random-string>
   DRIVE_WEBHOOK_ALLOW_UNVERIFIED=false
   ```
2. Deploy. On startup the backend seeds the cache once and calls `changes.watch`.
3. Confirm: `GET /api/cache/status` shows `push.active=true` and a warm `cached_at`.

Google requires a valid public SSL certificate (Railway provides this).

## Local (hybrid)

Without a public HTTPS URL, the backend still:

1. Seeds the in-memory cache on startup with one Drive `list_folder_files` call
2. Serves `GET /api/cache/files` from memory
3. Implements the webhook path fully for when a tunnel URL is set

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
3. Restart uvicorn. Logs should show `Drive push channel registered`.
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

- **Primary updates:** Google push → async cache refresh + DB `sync_file_list`
- **Startup:** one seed list into memory (DB sync deferred to webhook/fallback)
- **Fallback:** when auto-index is on, a rare full sync (15m without push, 6h with push)
- **Channel renew:** background loop re-calls `changes.watch` before expiry
