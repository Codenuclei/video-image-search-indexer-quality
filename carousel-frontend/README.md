# Carousel Studio (frontend)

Next.js app for Instagram-style carousel generation. Runs alongside the main
DriveFaceIndexer frontend (`frontend/`) against the **same FastAPI backend**.

## Backend URL (required)

| Environment | How the browser reaches the API |
|---|---|
| **Local** | `NEXT_PUBLIC_API_URL=/backend` (default). Next rewrites `/backend/*` → `http://127.0.0.1:8000` via `API_PROXY_TARGET`. |
| **Railway (production)** | Same same-origin `/backend` path. Build sets `API_PROXY_TARGET=https://dfi-backend-production.up.railway.app` so the Node server proxies to the Railway backend — no backend CORS change needed. |

Do **not** point the browser at a different backend than the one serving indexed videos/captions. Production must use the Railway backend above.

```bash
# Local (backend on :8000)
cp .env.local.example .env.local   # NEXT_PUBLIC_API_URL=/backend
npm run dev                        # http://localhost:3002
```

## Railway service

Service name: `dfi-carousel` (new service in the `drivefaceindexer` project — does not replace `dfi-frontend`).

Deploy from repo root (or via `scripts/auto-deploy.sh` when `carousel-frontend/` changes):

```bash
cd carousel-frontend && railway up --service dfi-carousel --detach -y
```

## Scripts

```bash
npm run dev      # :3002
npm run build
npm start        # :3002
```
