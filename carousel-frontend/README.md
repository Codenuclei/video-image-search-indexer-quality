# Carousel Studio (frontend)

Next.js app for Instagram-style carousel generation. Runs alongside the main
DriveFaceIndexer frontend (`frontend/`) against the **same FastAPI backend**.

## Backend URL (required)

| Environment | How the browser reaches the API |
|---|---|
| **Local** | `NEXT_PUBLIC_API_URL=/api/proxy` or `/backend` (both are App Router proxies to `API_PROXY_TARGET`, default `http://127.0.0.1:8000`). Durable for long extract/generate — not Next config rewrites (those die at ~30s). |
| **Railway (production)** | Same same-origin `/api/proxy` (or `/backend`) path. Build sets `API_PROXY_TARGET=https://dfi-carousel-backend-production.up.railway.app`. |

Do **not** point Studio at search (`dfi-backend`). Production Studio must use `dfi-carousel-backend`.

```bash
# Local (backend on :8000)
cp .env.local.example .env.local   # NEXT_PUBLIC_API_URL=/api/proxy
npm run dev                        # http://localhost:3002
```

## Railway service

Service name: `dfi-carousel` in the `drivefaceindexer` project (does not replace `dfi-frontend`). API service: `dfi-carousel-backend`.

**Root Directory must be `carousel-frontend`** (same pattern as `dfi-frontend` → `frontend`), with
`railwayConfigFile=/carousel-frontend/railway.json`. Building from the monorepo root makes Railpack fail.

```bash
# from repo root (Root Directory is already set on the service)
railway up --service dfi-carousel --detach -y
# GitHub: this service tracks origin/pruned-craousel (not main)
git push origin pruned-craousel
```

Or via `scripts/auto-deploy.sh` when `carousel-frontend/` changes.

## Scripts

```bash
npm run dev      # :3002
npm run build
npm start        # :3002
```
