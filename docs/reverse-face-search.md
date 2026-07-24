# Reverse Face Search — Release 1 architecture

## Decision

**Face vectors stay in Postgres pgvector** (`face_embeddings`). Qdrant remains for media (images / video frames / captions) only. Migrating faces to Qdrant is out of R1.

## Pipeline

```mermaid
flowchart LR
  Upload[UploadFace] --> Detect[ArcFaceEmbed]
  Detect --> PG[PostgresPgvector]
  PG --> People[MatchedPersons]
  People --> Apify[OptionalApifyLens]
  Apify --> LI[LinkedInMap]
```

| Stage | R1 implementation |
|-------|-------------------|
| Query | `POST /reid/faces/search` multipart upload → InsightFace → ANN → people |
| Enrich | Existing `FaceWebMatch` + `GET /reid/linkedin-map` (public LinkedIn only) |
| Crawl MVP | `POST /reid/faces/crawl` with public image URLs |
| Scrape | Existing Apify Google Lens for indexed `face_id` reverse-search |

## Out of scope

- Private / Sales Navigator LinkedIn resolver without a provided API
- Bulk open-web face scraping into a new identity graph
- Face collection migration to Qdrant
