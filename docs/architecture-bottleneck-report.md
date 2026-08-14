# Architecture Bottleneck Report

**System:** Video/Image Search Indexer and Carousel Studio  
**Audit date:** 2026-08-13  
**Status basis:** current local working tree plus confirmed production audit facts supplied for this report  
**Scope note:** “Locally implemented” means present in the current uncommitted working tree. It does **not** mean deployed. “Production-confirmed” means observed in the production audit.

## Executive summary

The most urgent constraint is not model throughput: it is storage lifecycle and data-plane design. The production `/app/data/videos` cache is at an apparent **83.135 GiB across 1,353 files** (`du -sh`: **84G**). Production has already exhibited an `ENOSPC` incident together with a Media-record shadowing regression. Failed work can then be requeued, which amplifies downloads and writes while the volume is already constrained.

The second critical constraint is video delivery. A cached video can be returned with `FileResponse`, but an uncached Drive preview is fully assembled in backend memory by `download_to_memory`; the Carousel Next.js proxy then fully buffers the upstream response again with `arrayBuffer`. That creates two full-body buffering points, weakens seek behavior, increases latency and memory pressure, and currently prevents safe eviction of processed Drive video copies. **Processed Drive cache should not be deleted until end-to-end HTTP Range streaming is verified.**

The third constraint is failure-domain coupling. Production currently combines API/search and indexing/background loops in the backend service. CPU, DB connections, Drive download slots, local disk writes, and process memory are therefore shared with user-facing requests. The local tree contains a `RUN_INDEXER` gate, leader election, batching, and a documented same-image Railway split, but that split is a **target/cutover option**, not a production-confirmed deployment.

Recommended order:

1. Stop disk growth and retry amplification; capture a manifest and preserve protected categories.
2. Implement and verify end-to-end Range streaming through FastAPI and Next.js.
3. Reclaim only eligible processed Drive cache, then conditionally reclaim failed Drive cache after remote-access and quiescence checks.
4. Split API and indexer into separate Railway services using the same image and shared stores.
5. Add explicit disk, queue, retry, Range, API latency, and cache-reconciliation telemetry.

## Evidence and confidence model

This report separates:

- **Production-confirmed facts:** topology and cache inventory supplied by the production audit; the Media shadowing regression; `ENOSPC`; and retry amplification.
- **Current-code facts:** behavior directly visible in the current working tree and referenced by path and line range.
- **Locally implemented/uncommitted changes:** code currently present in the working tree, including durable cache helpers, disk guards, permanent-library gates, embed/status batching, and API/indexer controls. These must not be described as deployed.
- **Recommendations:** target behavior, safeguards, and measurable acceptance criteria.

## Current Railway topology

Production currently consists of:

- **Backend / API + indexer combined:** FastAPI routes, Drive sync, indexing, maintenance, backup, search, and carousel orchestration share a service and runtime resources.
- **Indexer UI:** the main frontend for folder selection, queue/status, library, and search.
- **Carousel:** Next.js Carousel Studio and its same-origin backend proxy.
- **Connector:** Drive connector/direct-client boundary for metadata and media reads.
- **Postgres:** system-of-record metadata, statuses, relationships, runtime settings, and job state.
- **Qdrant:** image, caption, and video/transcript vector collections.
- **Persistent volume:** `/app/data`, including `/app/data/videos` and generated/indexing assets.

### System architecture

```text
 Users
   |----------------------|
   v                      v
 Indexer UI           Carousel (Next.js)
   |                      |
   |                      v
   |               same-origin API proxy
   |                      |
   |----------------------v
            Backend / API + Indexer  [combined in production]
              |       |       |       \
              |       |       |        \--> Persistent volume
              |       |       |              /app/data/videos
              |       |       |
              v       v       v
          Postgres  Qdrant  Drive connector/direct API
```

The FastAPI lifespan starts background leader work, auto-indexing, maintenance, and backup in the same application that serves routes (`backend/app/main.py:60-242`). `RUN_INDEXER=false` can suppress those loops (`backend/app/main.py:115-131`, `209-223`), but the approved split is documented as optional and pending cutover (`docs/indexer-service.md:1-49`).

## Ranked bottlenecks

### 1. Critical — persistent-volume exhaustion and unsafe cache lifecycle

**Production evidence**

- `/app/data/videos`: exact apparent size **83.135 GiB**, **1,353 files**; filesystem-oriented `du -sh` reports **84G**.
- Confirmed `ENOSPC` failure.
- Confirmed Media shadowing regression created a mismatch between logical processing state and durable media records.
- Case-variant duplicate copies account for **18.349 GiB**, but overlap the category totals and therefore must not be added again when reconciling total usage.

**Code evidence**

- Video paths are durable, ID-derived paths (`backend/app/video/youtube_cache.py`).
- Local durable cache code downloads via a temporary file, moves to a partial path on the target filesystem, then atomically replaces the final path (`backend/app/drive/media_cache.py:83-129`).
- Local disk guards reserve headroom and raise a retryable `ENOSPC`-class error before starting writes (`backend/app/storage.py:1-51`; `backend/app/pipelines/common.py:407-430`).
- The current local image path now uses durable cache (`backend/app/pipelines/image.py:35-50`), but this is uncommitted and not production-confirmed.

**Why it bottlenecks**

The cache has no production-confirmed enforced capacity, category-aware retention, or high-watermark eviction. Once the volume fills, downloads, temp files, cache publication, thumbnails, frames, and possibly database-adjacent maintenance fail together. Retrying cannot create capacity; it repeats costly I/O and can increase the number of partial or duplicate artifacts.

### 2. Critical — full-body media buffering blocks safe eviction

**Code evidence**

- An uncached Drive preview calls `download_to_memory` and returns a fully materialized `Response` (`backend/app/routers/drive.py:412-460`).
- `download_to_memory` accumulates every chunk in a list and joins all chunks into one bytes object (`backend/app/pipelines/common.py:155-163`).
- The Carousel proxy buffers the entire upstream body using `await upstream.arrayBuffer()` before constructing the downstream response (`carousel-frontend/lib/api-proxy.ts:31-73`).
- Cached videos use `FileResponse` and advertise `Accept-Ranges`, but the uncached remote path does not implement Range translation (`backend/app/routers/drive.py:424-459`).

**Why it bottlenecks**

Large media may exist simultaneously in backend chunk buffers, the joined backend bytes object, Node’s upstream body, the proxy `ArrayBuffer`, and browser buffering. Seeking may trigger repeated full transfers. Process memory and request latency scale with object size instead of chunk size.

**Retention consequence**

Processed Drive media is only eligible for deletion **after** verified Range streaming through every hop. Deleting it earlier would move user previews to the current full-download path and could create a memory/network regression.

### 3. High — API and indexing share a failure domain

**Code evidence**

- One FastAPI application exposes Drive, index, search, carousel, settings, YouTube, and other routes (`backend/app/main.py:245-316`).
- The same lifespan starts auto-index, maintenance, and backup tasks (`backend/app/main.py:209-225`).
- The worker holds parallel image/video/carousel tasks, DB concurrency, status batching, and an embed queue (`backend/app/workers/indexer.py:148-183`).
- The optional split prescribes `RUN_INDEXER=false` for API and a one-replica indexer using the same image (`docs/indexer-service.md:3-38`).

**Why it bottlenecks**

Index bursts compete with search and previews for CPU, memory, event-loop attention, DB pool slots, Drive slots, and volume I/O. A disk or process failure impacts both ingestion and serving. Leader election reduces duplicate background loops inside a deployment, but does not isolate resources.

### 4. High — retry amplification under persistent failures

**Production evidence**

The audit confirmed retries amplified the Media shadowing/`ENOSPC` incident.

**Code evidence**

- Auto-index can requeue errored and skipped rows every tick when runtime toggles are enabled (`backend/app/workers/auto_indexer.py:49-70`).
- Requeue resets status/error/decode attempt fields (`backend/app/workers/requeue_failed.py:71-150`).
- Transient DB errors are returned to `PENDING` (`backend/app/workers/indexer.py:121-139`).
- Carousel work has explicit bounded attempts, but general indexing retries are controlled through status and runtime policy rather than a unified resource-error circuit breaker (`backend/app/workers/indexer.py:81-88`).

**Why it bottlenecks**

A deterministic capacity failure must open a circuit, not re-enter the queue. Without resource-specific suppression and exponential backoff, retries multiply remote reads, temp writes, DB updates, logs, and contention.

### 5. High — logical Media state and physical cache can diverge

**Production evidence**

The confirmed Media shadowing regression left Drive rows and physical video files in inconsistent categories, including ERROR/no-Media files that still consumed **41.415 GiB**.

**Code evidence**

- Media rows can be cleared before replacement (`backend/app/pipelines/common.py:143-147`; called by image/video pipelines).
- Local recovery code restores processed status where Media already exists (`backend/app/drive/cleanup.py`; `backend/app/workers/indexer.py:1439`, `1633`).
- Cache identity and database metadata are separate: physical cache resolution uses `cache_rel_path` and ID-derived fallbacks (`backend/app/drive/media_cache.py:53-80`).

**Why it bottlenecks**

Database status alone is not sufficient to decide whether a physical file is live, reproducible, orphaned, or safe to delete. Cleanup must reconcile Drive ID, source, status, Media relation, active jobs, physical path, size, and normalized filename case.

### 6. Medium — high concurrency magnifies downstream contention

**Code evidence**

- The worker supports many parallel image jobs and DB-held phases (`backend/app/workers/indexer.py:163-183`, `219-246`).
- Status writes and embeddings are locally batched (`backend/app/workers/index_batch.py`; `backend/app/workers/embed_queue.py`).
- The proposed indexer profile raises image and Drive parallelism and uses a large DB pool (`docs/indexer-service.md:15-30`).

**Why it bottlenecks**

Concurrency improves throughput only while Drive, disk, Gemini, DB, and Qdrant remain below saturation. With a nearly full volume or overloaded DB, higher concurrency increases outstanding bytes and rollback/retry work. Concurrency should be governed by disk headroom and dependency latency, not configured as static maximums alone.

### 7. Medium — carousel orchestration remains backend-heavy

**Code evidence**

- The backend owns carousel extraction, theme/script generation, frame selection, translation, and serialized locks (`backend/app/routers/carousel_script.py`; `backend/app/search/carousel_pipeline.py`; `backend/app/search/carousel_frame_select.py`).
- Local comments document prior remount/retry storms and serialize expensive extracts/generation (`backend/app/routers/carousel_script.py:62`, `1304`, `1450`).
- Browser frame capture can perform additional media reads (`carousel-frontend/lib/browser-frame-capture.ts`).

**Why it bottlenecks**

Interactive requests and long-running generation compete with indexing when they share the backend. Duplicate client actions require idempotency keys and persisted job state, not only in-process serialization.

## Confirmed production cache inventory

All GiB values below use the production audit’s apparent-size accounting. Duplicate case-variant bytes overlap category totals.

| Category | Apparent GiB | Physical files | Logical IDs | Retention decision |
|---|---:|---:|---:|---|
| Drive ERROR / no Media | 41.415 | 1,024 | not separately confirmed | Conditional cleanup only |
| Drive PROCESSED / Media | 36.433 | 307 | 182 | Eligible only after Range streaming |
| YouTube PROCESSED / Media | 3.671 | 7 | 7 | KEEP |
| Drive SKIPPED / Media | 1.387 | 4 | 4 presumed; verify manifest | Treat as protected until reviewed |
| Unknown / orphan | 0.229 | 2 | unknown | KEEP pending review |
| **Total `/app/data/videos`** | **83.135** | **1,353** | mixed | Audit baseline |
| Duplicate case-variant copies | 18.349 | included above | overlapping | Deduplicate only by verified identity |

The category rows sum to 83.135 GiB. The **18.349 GiB duplicate estimate is an overlap analysis, not additional usage**. Deduplication must use content hash or confirmed Drive identity and must avoid deleting the only path referenced by a live row.

## Safe retention policy

### Always KEEP

- Upload-origin media.
- YouTube media.
- Unknown/orphan files until manually classified.
- Any file referenced by active indexing, preview, carousel, repair, or migration work.
- Partial files whose owning operation is demonstrably active.
- Drive SKIPPED/Media until the reason and downstream dependencies are reviewed.

### Eligible after Range streaming

- Processed Drive media with a valid Media record, only after:
  1. backend supports and validates `Range`,
  2. connector/Drive requests fetch only requested byte spans,
  3. Next proxy streams without `arrayBuffer`,
  4. browser seeking returns `206` and bounded bytes,
  5. rollback can restore cache-first serving.

### Conditional after remote-access verification and quiescence

- Failed Drive cache with no Media relation, only when:
  - the Drive object still exists and is readable,
  - no active/retry job owns it,
  - the error queue is paused or quiescent,
  - a manifest records path, size, source, Drive ID, status, Media count, hash when practical, and deletion reason,
  - deletion is rate-limited and stops on reconciliation mismatch.

### Never infer deletion safety from filename case alone

Case-variant copies may represent duplicates, stale references, or platform naming behavior. Hash or stable source identity first; update references atomically; retain a rollback manifest.

## Failure narrative: shadowing, ENOSPC, and retries

The production incident is best understood as a loop:

```text
 Media relationship/state regression
          |
          v
 logical row appears incomplete or failed
          |
          v
 retry/requeue schedules download and processing
          |
          v
 additional temp/cache writes on nearly full volume
          |
          v
 ENOSPC -> failed processing / incomplete logical state
          |
          +-----------------------> retry/requeue
```

The response must break the loop at the resource boundary:

- classify `ENOSPC` and disk-reserve failures as capacity-blocked;
- pause new cache writes and automatic retries;
- preserve the original logical relationship until replacement is committed;
- reconcile physical files before requeueing;
- only resume after free-space and protected-category checks pass.

Local code includes a pre-write disk guard and atomic cache publication (`backend/app/storage.py`; `backend/app/drive/media_cache.py`), but production behavior must be verified after deployment before treating those safeguards as active.

## Indexing pipeline

```text
 Drive push / fallback sync
          |
          v
 Postgres DriveFile upsert / claim
          |
      +---+--------------------+
      |                        |
      v                        v
 image pipeline            video pipeline
 cache -> decode           cache -> transcript/frames
 -> faces/caption          -> segments/captions
      |                        |
      +------------+-----------+
                   v
          embeddings / Qdrant
                   |
                   v
        batched final status -> Postgres
                   |
                   v
        background carousel generation
```

The local tree uses claim ordering, advisory locks, status batching, and an image embed queue. Those are throughput and consistency improvements, not substitutes for capacity control.

## Media retention and streaming target

```text
 Browser sends Range: bytes=start-end
                   |
                   v
 Carousel Next proxy (pass through headers; stream body)
                   |
                   v
 FastAPI preview (validate range; return 206)
             |                         |
       cached local file          uncached Drive object
       bounded file read          remote bounded range read
             |                         |
             +------------+------------+
                          v
               Content-Range / length

 Reconciler: DB rows + active jobs + cache manifest -> retain / eligible / quarantine
```

Required HTTP behavior:

- valid single ranges return `206 Partial Content`;
- `Content-Range`, `Content-Length`, `Accept-Ranges`, MIME type, ETag/version where available, and `HEAD` are coherent;
- unsatisfiable ranges return `416`;
- proxy preserves status and range headers and streams the body;
- no hop buffers the full object for a bounded range.

## Current versus target API/indexer split

```text
 CURRENT (production-confirmed)          TARGET

 Users                                  Users
   |                                      |
   v                                      v
 Backend/API + Indexer                  API service (RUN_INDEXER=false)
   |  |  |                                |  |  \
   |  |  +-- volume                       |  |   \-- optional read cache
   |  +----- Qdrant                       |  +------ Qdrant
   +-------- Postgres                     +--------- Postgres
                                                    ^
                                                    |
                                      Indexer service (1 replica)
                                      RUN_INDEXER=true
                                      Postgres + Qdrant + volume + Drive
```

The same image can support both roles. Separate Railway services provide CPU/memory/process isolation; shared Postgres and Qdrant preserve coordination. Volume semantics must be decided explicitly: if both services need direct file paths, the platform must support the required shared mount behavior; otherwise the API should stream from Drive/object storage and the indexer alone should own write cache.

## Carousel pipeline

```text
 Topic / source selection
          |
          v
 transcript + semantic moments (Postgres/Qdrant)
          |
          v
 hook/theme/script generation (LLM)
          |
          v
 candidate frame scoring and selection
          |
          v
 media preview / browser frame capture
          |
          v
 slide composition -> caption/export/save
```

Target controls:

- one persisted generation job per idempotency key;
- explicit status and cancellation;
- bounded concurrency per expensive stage;
- cached transcript/moment/frame metadata;
- Range-based video access;
- stage latency and error metrics.

## Metrics and acceptance targets

### Storage and retention

- Alert at **70%** volume use; stop nonessential cache population at **80%**; hard circuit-breaker at **85%**.
- Maintain at least **max(10 GiB, 15% of volume)** free before accepting new large durable writes.
- Cache inventory reconciliation: **100% of files classified** by source/status/Media/active ownership; unknown files never auto-deleted.
- Partial files older than the active-job TTL: **0**, after quarantine/reconciliation.
- Duplicate reclaim reports physical bytes without double-counting category totals.

### Streaming

- A 1 MiB browser range yields `206`, transfers at most **1.1 MiB plus protocol overhead** at each application hop, and does not allocate the full object.
- Seek startup p95: **<2 seconds** for cached media and **<4 seconds** for remote Drive media under normal dependency health.
- Proxy and backend preserve `Range`, `Content-Range`, `Content-Length`, MIME, and status in automated integration tests.
- Full-body `arrayBuffer` is absent from media proxy paths.

### API/indexer isolation

- API `/health` remains available during index saturation.
- Search API p95: **<750 ms excluding explicitly long LLM generation**, with <1% 5xx over a 15-minute load window.
- Indexer restart or crash does not restart the API service.
- API role runs with `RUN_INDEXER=false`; indexer role is one replica with leader status confirmed.

### Queue and retries

- Disk-capacity failures trigger **0 automatic re-downloads** until the disk circuit closes.
- Retry count is bounded and visible by error class; retry age and next-attempt timestamp are queryable.
- No status oscillation between ERROR/PENDING caused by unchanged deterministic failures.
- Queue depth, oldest age, throughput, and per-stage failure rate are exported.

### Data consistency

- Every PROCESSED row has the required Media/vector artifacts or a visible reconciliation exception.
- Deleting an eligible cache file does not delete Postgres Media metadata or Qdrant vectors.
- A dry-run manifest and post-run manifest agree on bytes/files within expected concurrent-write tolerances.

## Phased remediation roadmap

### Phase 0 — contain and observe (immediate)

1. Pause automatic retries for capacity-related failures and pause nonessential new indexing if free space is below threshold.
2. Snapshot Postgres and create a read-only file manifest before deletion.
3. Mark upload, YouTube, unknown, active, and unreviewed skipped media as protected.
4. Record disk use, inode use, queue depth, ERROR reasons, active jobs, and cache reconciliation counts.
5. Deploy nothing implicitly: review and test the local disk/cache safeguards first.

**Exit:** stable free space, no retry-driven growth, complete inventory manifest, protected-set sign-off.

### Phase 1 — fix media delivery

1. Add FastAPI single-range support for cached paths and remote Drive reads.
2. Stream the Next.js proxy response and preserve Range headers/status.
3. Add `HEAD`, `206`, `416`, seek, cancellation, and large-object memory tests.
4. Canary with one large Drive video and monitor backend/Node RSS and transferred bytes.

**Exit:** all streaming acceptance targets pass in production canary.

### Phase 2 — reclaim safely

1. Dry-run processed Drive eviction against the reconciled manifest.
2. Delete in small batches with free-space and mismatch stop conditions.
3. Recheck preview/seek after each batch.
4. For ERROR/no-Media cache, verify remote readability and retry quiescence before conditional deletion.
5. Deduplicate case variants only by verified identity/hash and reference migration.

**Potential ceiling:** processed Drive + failed Drive categories total **77.848 GiB**, but this is not an immediate reclaim promise. Protected, active, non-reproducible, and verification-failed files must remain.

### Phase 3 — isolate API and indexing

1. Deploy the same image as an API service with `RUN_INDEXER=false`.
2. Deploy one indexer replica with `RUN_INDEXER=true`.
3. Verify shared stores, leader ownership, queue draining, health, and independent restarts.
4. Tune concurrency from observed DB/Drive/Qdrant/disk saturation, not from maximum throughput alone.

**Exit:** index load does not violate API latency/error targets; roles restart independently.

### Phase 4 — harden lifecycle and operations

1. Implement capacity-aware cache policy with high/low watermarks and protected categories.
2. Add persisted retry scheduling, error-class circuit breakers, and operator controls.
3. Run daily cache/DB/Qdrant reconciliation with dry-run and audit log.
4. Add idempotent persisted carousel jobs and stage metrics.
5. Exercise restore, cache rebuild, Drive outage, Qdrant outage, and full-volume runbooks.

## Decision log

- **Do not delete processed Drive cache yet.** Range streaming is the gating dependency.
- **Keep upload, YouTube, unknown, and active files.**
- **Failed Drive files are conditional, not automatically disposable.**
- **Do not add duplicate-case bytes to category totals.**
- **Treat local safeguards and split controls as uncommitted until deployed and observed.**
- **Prefer one indexer replica initially** because local batching/ownership assumptions are documented around a single owner.

## Appendix A — evidence paths

### Service lifecycle and topology

- `backend/app/main.py` — FastAPI routes, deferred boot, leader election, index/maintenance/backup loops, `RUN_INDEXER`.
- `backend/app/config.py` — role and concurrency settings.
- `docs/indexer-service.md` — optional Railway API/indexer split and cutover checklist.
- `backend/app/workers/auto_indexer.py` — periodic processing, retries, maintenance, Drive refresh.
- `backend/app/workers/indexer.py` — task concurrency, status batching, embed queue, recovery.

### Storage and cache

- `backend/app/drive/media_cache.py` — local durable cache, partial file, atomic publication.
- `backend/app/storage.py` — local free-space guard.
- `backend/app/video/youtube_cache.py` — video path identity.
- `backend/app/pipelines/image.py` — local image cache use.
- `backend/app/pipelines/video.py` — video cache/index path.
- `backend/app/pipelines/common.py` — full-memory download, temp download, Media clearing.
- `backend/app/drive/cleanup.py` — local permanent-library/recovery logic.

### Streaming and proxy

- `backend/app/routers/drive.py:412-500` — cached `FileResponse`; uncached full-memory preview/download.
- `carousel-frontend/lib/api-proxy.ts:31-73` — same-origin proxy and response `arrayBuffer`.
- `carousel-frontend/app/api/proxy/[...path]/route.ts` — proxy route.

### Data and vectors

- `backend/app/db/models.py` — DriveFile, Media, statuses, and relationships.
- `backend/app/db/schema.py` — schema/recovery.
- `backend/app/qdrant/client.py` and collection modules — vector persistence.
- `backend/app/qdrant/video_transcripts.py` — local video transcript vector support.

### Carousel

- `backend/app/routers/carousel_script.py` — orchestration, serialization, generation endpoints.
- `backend/app/search/carousel_pipeline.py` — carousel generation pipeline.
- `backend/app/search/carousel_frame_select.py` — frame ranking.
- `backend/app/search/moments.py` — semantic moments.
- `carousel-frontend/app/carousel/page.tsx` — studio UI.
- `carousel-frontend/lib/browser-frame-capture.ts` — browser-side frame extraction.

## Appendix B — reproducible generation

From repository root:

```bash
node scripts/render-architecture-report.mjs
```

The renderer reads only local files, launches the Playwright Chromium already installed under `carousel-frontend`, applies deterministic A4 print settings, and writes:

```text
docs/artifacts/architecture-bottleneck-report.pdf
```

The printable source is `docs/architecture-bottleneck-report.html`; the analytical source of truth is this Markdown file. The HTML intentionally contains rendered HTML/CSS diagrams rather than raw Mermaid so PDF generation has no network or CDN dependency.
