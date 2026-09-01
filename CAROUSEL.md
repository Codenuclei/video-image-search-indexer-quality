# pruned-craousel

This clone is the **Carousel Studio** backend + UI. Drive-search HTTP (Qdrant image/moment search, clusters, reid, admin Google ID login) is unmounted.

## Auth (separate from Drive search)

- OAuth allowlist is `CAROUSEL_FRONTEND_URL` + `ALLOWED_ORIGINS` only.
- Default return is `/carousel`, not the search app `/folders`.
- Search frontend origins are rejected.
- `/auth/google-id-token` (admin GIS login) is not mounted.
- Use a **distinct** `GOOGLE_REDIRECT_URI` from `dfi-backend` (this service’s callback). Tokens live in **this** Postgres (`carousel` DB), not the search DB.

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
