#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Starting Postgres (Docker) ==="
docker start dfi-postgres 2>/dev/null || \
  docker run -d --name dfi-postgres -p 5432:5432 \
    -e POSTGRES_USER=drivefaceindexer \
    -e POSTGRES_PASSWORD=drivefaceindexer \
    -e POSTGRES_DB=drivefaceindexer \
    pgvector/pgvector:pg16

echo "=== Backend setup ==="
cd "$ROOT/backend"
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt -q
[ -f .env ] || cp .env.example .env
./.venv/bin/alembic upgrade head || true

echo "=== Frontend setup ==="
cd "$ROOT/frontend"
bun install
[ -f .env.local ] || cp .env.example .env.local

echo ""
echo "Done! Start:"
echo "  1. Drive Connector:  cd '../drive connector' && bun start"
echo "  2. Backend:          cd backend && .venv/bin/uvicorn app.main:app --port 8000"
echo "  3. Frontend:         cd frontend && bun run dev"
echo "  Open http://localhost:3001"
