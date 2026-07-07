# DriveFaceIndexer

Recursively index a Google Drive folder, detect faces in images/videos/PDFs, cluster unknown people, and auto-recognize them after you name someone once.

Uses your existing **Drive Connector** (`drive connector/`) for Google OAuth + file access, **InsightFace** for detection/embeddings, **PostgreSQL + pgvector** for similarity search, and a **Next.js** dashboard for review.

## Architecture

```
Google Drive  →  Drive Connector (Node)  →  FastAPI Backend  →  Postgres+pgvector
                                              ↓
                                         InsightFace (CPU)
                                         FFmpeg (video frames)
                                         PyMuPDF (PDF text + faces)
                                              ↓
                                         Next.js Dashboard
```

## Quick start (local dev)

### Prerequisites

- Docker Desktop (for Postgres)
- Python 3.12 + venv
- Node 18+ or Bun
- ffmpeg on PATH
- Drive Connector configured (`drive connector/.env` with Google OAuth + API key)

### 1. Start Postgres

```powershell
docker run -d --name dfi-postgres -p 5432:5432 `
  -e POSTGRES_USER=drivefaceindexer -e POSTGRES_PASSWORD=drivefaceindexer `
  -e POSTGRES_DB=drivefaceindexer pgvector/pgvector:pg16
```

### 2. Start Drive Connector

```powershell
cd "..\drive connector"
bun install && bun start
# Sign in at http://localhost:3000, pick a folder, copy API key from data/store.json
```

### 3. Start Backend

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # fill DRIVE_CONNECTOR_API_KEY
.\.venv\Scripts\alembic upgrade head
.\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 4. Start Frontend

```powershell
cd frontend
bun install
bun run dev
# Open http://localhost:3001
```

### 5. Index your Drive

1. Open **Folders** → click **Start Index** (or enable **auto indexing** in Settings)
2. Open **Review Queue** → name unknown faces
3. Future appearances auto-tag

### Auto indexing

Turn on **Settings → Automatically sync Drive and index new or changed files**. The backend checks your connected folder on a timer (default every 5 minutes) and processes anything new or modified — no need to click Start Index each time.

Or set in `backend/.env` before starting the backend:

```
AUTO_INDEX_ENABLED=true
AUTO_INDEX_INTERVAL_SECONDS=300
```

## Docker Compose (full stack)

```powershell
# Set your connector API key (from drive connector after OAuth)
$env:DRIVE_CONNECTOR_API_KEY="dck_..."

docker compose up --build
```

| Service    | URL                      |
|------------|--------------------------|
| Frontend   | http://localhost:3001    |
| Backend    | http://localhost:8000    |
| API docs   | http://localhost:8000/docs |
| Connector  | http://localhost:3000    |

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET/POST | `/index` | Index status / trigger run |
| POST | `/reindex` | Force reprocess all files |
| GET | `/drive/files` | Tracked Drive files |
| GET | `/clusters` | Unknown-face review queue |
| POST | `/clusters/{id}/name` | Name a cluster |
| POST | `/clusters/{id}/ignore` | Ignore forever |
| POST | `/clusters/{id}/merge` | Merge into existing person |
| GET | `/persons` | Named people |
| GET | `/media` | Indexed media |
| GET | `/faces` | Detected faces |
| GET | `/faces/{id}/thumbnail` | Face crop JPEG |
| GET | `/search?q=` | Search people/files/OCR |
| GET/PUT | `/settings` | Matching thresholds |

## Testing

```powershell
cd backend
$env:TEST_DATABASE_URL="postgresql+asyncpg://drivefaceindexer:drivefaceindexer@localhost:5432/drivefaceindexer_test"
.\.venv\Scripts\python -m pytest -v
```

Place sample face photos in `backend/tests/fixtures/faces/` (gitignored) for real InsightFace integration tests.

## Project structure

```
DriveFaceIndexer/
├── backend/          FastAPI + InsightFace + pipelines
├── frontend/         Next.js dashboard
├── docker-compose.yml
└── README.md

drive connector/      Existing Node.js Drive OAuth connector (sibling repo)
```

## Notes

- **Memory**: Processes one file at a time; videos stream to disk; PDFs page-by-page.
- **PaddleOCR**: Optional fallback for scanned PDFs. Works in Docker/Linux; may be unavailable on native Windows (PyMuPDF native text still works).
- **Thresholds**: Tune via Settings UI or `PERSON_MATCH_THRESHOLD` / `CLUSTER_MATCH_THRESHOLD` env vars.
