# Golang indexer POC (decision gate)

## Goal

Measure whether a Go worker claiming PENDING image jobs via the existing Python HTTP API can beat Python `image_index_max_parallel` by **>2×** throughput with stable memory.

## How it works

1. Settings → **Go indexer canary** ON (`go_indexer_enabled`).
2. Go sidecar heartbeats + claims via `/index/go/*`.
3. Go downloads bytes (timing), then `POST /index/go/complete/{id}` runs the **Python** face/embed pipeline.
4. While Go is alive, Python reserves image slots and **does not adopt** Go-owned `PROCESSING` rows.

## Commands

```bash
cd scripts/go-indexer-poc

# Protocol check (toggle must be ON)
go run . -check -api https://dfi-backend-production.up.railway.app

# Canary batch
go run . -n 20 -api https://dfi-backend-production.up.railway.app -parallel 2
```

Local API default: `http://127.0.0.1:8002`.

## Go / no-go

| Result | Decision |
|--------|----------|
| ≥2× images/min and RSS stable | Prioritise Go rewrite spike |
| <2× or unstable | Keep Python indexer; tune concurrency / size-order instead |

## R1 status

**No production Go rewrite in Release 1.** Indexing compute stays in Python (`IndexingWorker`). This harness validates claim/download/complete + slot cooperation.

### Results (fill in)

- Date:
- Python images/min:
- Go images/min:
- Peak RSS Python / Go:
- Decision:
