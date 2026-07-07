# DriveFaceIndexer local install (Windows)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"

Write-Host "=== Starting Postgres (Docker) ===" -ForegroundColor Cyan
docker start dfi-postgres 2>$null
if ($LASTEXITCODE -ne 0) {
  docker run -d --name dfi-postgres -p 5432:5432 `
    -e POSTGRES_USER=drivefaceindexer -e POSTGRES_PASSWORD=drivefaceindexer `
    -e POSTGRES_DB=drivefaceindexer pgvector/pgvector:pg16
}

Write-Host "=== Backend setup ===" -ForegroundColor Cyan
Push-Location $Backend
if (-not (Test-Path ".venv")) { python -m venv .venv }
.\.venv\Scripts\pip install -r requirements.txt -q
if (-not (Test-Path ".env")) { Copy-Item .env.example .env; Write-Host "Created .env — fill DRIVE_CONNECTOR_API_KEY" -ForegroundColor Yellow }
.\.venv\Scripts\alembic upgrade head 2>$null
if ($LASTEXITCODE -ne 0) {
  .\.venv\Scripts\python -c "import asyncio; from sqlalchemy import text; from sqlalchemy.ext.asyncio import create_async_engine; from app.db.base import Base; from app.db import models; from app.config import get_settings; exec('''
async def m():
    e = create_async_engine(get_settings().database_url)
    async with e.begin() as c:
        await c.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        await c.run_sync(Base.metadata.create_all)
    await e.dispose()
asyncio.run(m())
''')"
}
Pop-Location

Write-Host "=== Frontend setup ===" -ForegroundColor Cyan
Push-Location $Frontend
if (-not (Test-Path "node_modules")) { bun install }
if (-not (Test-Path ".env.local")) { Copy-Item .env.example .env.local }
Pop-Location

Write-Host ""
Write-Host "Done! Start services:" -ForegroundColor Green
Write-Host "  1. Drive Connector:  cd '..\drive connector' && bun start"
Write-Host "  2. Backend:          cd backend && .\.venv\Scripts\python -m uvicorn app.main:app --port 8000"
Write-Host "  3. Frontend:         cd frontend && bun run dev"
Write-Host "  Open http://localhost:3001"
