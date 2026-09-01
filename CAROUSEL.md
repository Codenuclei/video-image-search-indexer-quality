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
