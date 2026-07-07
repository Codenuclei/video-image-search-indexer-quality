# DriveFaceIndexer — Gemini File Search Architecture

Managed multimodal RAG using **Google Gemini File Search** (Option 1). No local OCR, CLIP, InsightFace, or pgvector embeddings.

## Flow

```
Drive Connector → download files → Gemini File Search Store (gemini-embedding-2)
                                              ↓
User query → Gemini model + file_search tool → answer + citations
```

## What each component does

| Piece | Role |
|-------|------|
| **Drive Connector** | OAuth, folder listing, file download |
| **DriveFaceIndexer backend** | Sync Drive metadata, upload supported files to Gemini store |
| **Gemini File Search** | Chunk, embed (text + images), index, retrieve, answer |
| **Frontend** | Index status, folders, multimodal search UI |

## Supported file types

- Images: JPEG, PNG, WebP
- Documents: PDF, plain text, markdown, CSV
- Videos/audio: skipped (not supported by File Search yet)

## Setup

```powershell
# 1. Drive Connector
cd "c:\Users\MasterUnion\drive connector"
bun start

# 2. Backend — set GEMINI_API_KEY in backend/.env
cd c:\Users\MasterUnion\DriveFaceIndexer\backend
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000

# 3. Frontend
cd c:\Users\MasterUnion\DriveFaceIndexer\frontend
bun run dev
```

## Search examples

- `the boy` — finds text in scanned PDFs (Gemini reads page images)
- `people partying` — visual + semantic match in photos
- `what happens in the wimpy kid book` — document Q&A with citations

## Pricing

Gemini File Search: free storage/query embeddings; ~$0.15 per 1M tokens on initial file indexing.
