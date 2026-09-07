# pruned-craousel

This clone is the **Carousel Studio** backend + UI. Drive-search HTTP (Qdrant image/moment search, clusters, reid) is unmounted.

## Auth (this backend is the auth server)

GIS login and Drive OAuth are **copied into this process**. Studio does not call search (`dfi-backend`) for tokens, sessions, or Google client verification.

- GIS: `POST /auth/google-id-token`, `GET /auth/is-admin` — `backend/app/routers/carousel_auth.py`. Allowlist is `app_admins` in **this** Postgres.
- Drive OAuth: `/auth/google`, `/auth/google/callback`, `/api/session`, `/api/drive-token` — `backend/app/routers/carousel_oauth.py`. Tokens live in **this** `carousel` DB.
- Credentials: prefer `CAROUSEL_GOOGLE_CLIENT_ID` / `SECRET` / `REDIRECT_URI` / `API_KEY`. Empty values fall back to `GOOGLE_*` in the **same** process only.
- OAuth allowlist is `CAROUSEL_FRONTEND_URL` + `ALLOWED_ORIGINS`. Default return is `/carousel`. Search frontend origins are rejected.
- Register a **distinct** Google redirect URI for this service’s callback (not search’s).

## Hour-long videos

- Ingest is **one video at a time** (`VIDEO_INDEX_MAX_PARALLEL=1`).
- Drive sync is **video-only**. Photos, HEIC, PDFs, and other non-video files are not queued or indexed. Slide stills still come from ffmpeg on the video.
- Frame dump is **sparse** (8 samples across the file), not 1 fps. Slide stills are ffmpeg seeks after timestamps exist.
- Whisper audio extract timeout is **3600s**. Index stall watchdog is **7200s**.
- Gunicorn: **1 worker**, timeout **3600s**.
- Temp mp4 stays in `/tmp` during ingest; do not keep a media volume for the library.

## Run

```bash
# API (from carousel-backend clone: this repo's backend/)
cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000

# Studio
cd carousel-frontend && npm run dev   # :3002, proxy to :8000
```

Postgres default database name: `carousel`.

## Railway (production)

Isolated services in project `drivefaceindexer` (do not share search Postgres / search `qdrant` / `dfi-backend`):

- API: `dfi-carousel-backend` → https://dfi-carousel-backend-production.up.railway.app
- DB: `Postgres-WEBK` (referenced only by the Studio API)
- Vectors: `dfi-carousel-qdrant` (`qdrant/qdrant:latest`, volume `/qdrant/storage`, private only). API `QDRANT_URL=http://dfi-carousel-qdrant.railway.internal:6333`
- UI: `dfi-carousel` → https://dfi-carousel-production.up.railway.app (`API_PROXY_TARGET` = the API URL above)

Deploy from branch `pruned-craousel` (GitHub on `dfi-carousel` + `dfi-carousel-backend`; never `main`):

```bash
git push origin pruned-craousel
# or local upload:
cd backend && python -m pytest tests/test_import_guards.py -q
cd backend && railway up --service dfi-carousel-backend --detach -y
```

Google Cloud: add authorized redirect URI `https://dfi-carousel-backend-production.up.railway.app/auth/google/callback` (keep search’s `dfi-backend` callback). API key referrer: `https://dfi-carousel-production.up.railway.app/*`.
