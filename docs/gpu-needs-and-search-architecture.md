# GPU Needs & Search Architecture

**System:** DriveFaceIndexer (video / image search + Carousel Studio)  
**Date:** 2026-09-02  
**Audience:** product, ops, engineering decision-makers  
**PDF:** [`docs/artifacts/gpu-needs-and-search-architecture.pdf`](artifacts/gpu-needs-and-search-architecture.pdf)  
**Related:** `docs/full-system-architecture-and-risks.md` · `docs/quality-blockers-and-subscriptions.md` · `docs/indexer-service.md`

---

## 1. Executive decision

| Decision | Detail |
|---|---|
| **Buy GPU** | For **InsightFace / ArcFace** first (`CUDAExecutionProvider`). Optional second GPU/VLM later. |
| **Keep Railway CPU** | API, indexer, Postgres adjacency, Qdrant, Drive I/O, Whisper, ffmpeg, RapidOCR. |
| **Keep API model spend** | Gemini (embed / caption / VLM), Claude (carousel), OpenRouter (failover). Not a substitute for face GPU. |
| **Do not** | Treat more Railway CPU face replicas as the final face plan. Do not put user-facing API or Postgres on the GPU box. |
| **Search safety** | Default production `/search` stays until golden eval says promote. Experiments stay isolated. |

**One line:** Other companies win visual search with *retrieve → structure → top-K judge*. We align to that: **GPU for faces**, Railway for serve/index, OCR/object/caption structure before promotion, VLM only on a shortlist.

---

## 2. Why this brief exists

Three overlapping pressures:

1. **People search & indexing throughput** — InsightFace on CPU (ONNX `CPUExecutionProvider`) is the face saturator. Railway has no GPU; scaling CPU replicas is a workaround.
2. **Search relevance** — Gemini visual ANN is coarse recall. Apparel/brand and gym lookalikes feel “meaningless” when ANN scores are treated as precision. Industry stacks never stop at ANN.
3. **Architecture cutover** — API, indexer, and face workers must separate failure domains while we add GPU faces, OCR geometry, and a budgeted top-K judge — without harming live `/search`.

This document is the shared plan for compute purchase + search architecture, not a vendor pitch.

---

## 3. What other companies actually do

### 3.1 The industry stack (Lens / Pinterest / Shopify / modern multimodal RAG)

Mature visual search is almost never “embed the query → return nearest neighbors.”

| Stage | What happens | Cost | Role |
|---|---|---|---|
| **A. Cheap retrieve** | Bi-encoder ANN (CLIP / SigLIP / multimodal embed) pulls hundreds of candidates from a vector index | Low | **Recall prior** only |
| **B. Structure** | OCR boxes, object/apparel attributes, face IDs, metadata, lexical fuse | Medium | Disambiguate what ANN cannot |
| **C. Expensive judge** | Cross-encoder or VLM scores *query ↔ candidate* on top ~20–100 | High | **Precision** |
| **D. Product policy** | Hard negatives, diversity, person/role gates, brand-on-garment rules | Low | Product truth |

Needing a VLM on the top of a visually similar shortlist is **normal**. Shipping ANN alone would look equally “meaningless” at Google or Pinterest for “logo on a tee vs logo on a banner.”

### 3.2 What embeddings can and cannot do

| Embeddings are good at | Embeddings are bad at |
|---|---|
| Pulling “gym / campus / party-ish” lookalikes | Identity of near-duplicate objects (sandbag vs medicine ball; ski-erg vs rower) |
| Broad scene neighborhood | Brand *location* (tee print vs backdrop) |
| Cheap corpus-wide recall | Saying yes/no to a specific natural-language claim |

So: Gemini `embedding-2` stays. We stop treating mid-score visual similarity as the answer.

### 3.3 How DriveFaceIndexer maps today

| Industry block | Our equivalent | Gap |
|---|---|---|
| Visual ANN recall | Qdrant + Gemini embedding-2 | Over-trusted as ranker |
| Caption / text index | Caption ANN + lexical + stored captions | Caption-first fusion prototyped; not default |
| Object / attribute gates | Object labels + apparel-brand association + hard-negatives | Backdrop brand still co-mentioned in captions |
| OCR localization | RapidOCR ONNX lane (opt-in, idle workers) | Region heuristics incomplete (many spans → `other`) |
| Face identity | InsightFace `buffalo_l` CPU ONNX on `dfi-face-worker` | Needs CUDA GPU |
| Top-K multimodal judge | Caption LLM filter / Gemini VLM paths | Not a true pixel cross-encoder on a shortlist |
| Eval / regression SLO | `eval_search_quality.py` + small golden set; `caption_queries_500.csv` unwired | **B8** — no live deploy gate |

### 3.4 Failure modes that only structure + judge fix

| Failure | Why ANN / captions fail | What fixes it |
|---|---|---|
| Brand on backdrop ranked as on-garment | Caption says both “hat” and “mastersunion” | OCR boxes → brand near torso/garment, not banner |
| Sandbag vs medicine ball | Shape neighbors in embed space | Better object tags and/or top-K pixel yes/no |
| Ski-erg vs rower | Same | Same |
| Missing faces in people search | CPU InsightFace backlog / incomplete index | GPU face fleet + coverage SLO |

---

## 4. Current system (baseline)

### 4.1 Topology (simplified)

```text
Users → Indexer UI / Carousel
     → dfi-backend   (API; historically coupled to index work)
     → dfi-face-worker (CPU InsightFace + idle OCR/object lanes)
     → Postgres (system of record)
     → Qdrant (rebuildable vectors)
     → /app/data volume (cache, frames, uploads)
     → Drive + Gemini + Claude / OpenRouter
```

Documented target split (same Docker image): `dfi-backend` (API only) · `dfi-indexer` (volume, downloads, Whisper, ffmpeg, enqueue) · `dfi-face-worker` fleet (volume-less today). See `docs/indexer-service.md`.

### 4.2 Search pipeline (images)

```text
Query
  → cache (exact / semantic)
  → resolve persons / roles / visual strip
  → route by type (video | person | object/apparel/scene)
  → local DB + Qdrant ANN (visual + caption + objects)
  → attach captions
  → object / apparel / action hard-negative gates
  → optional caption LLM / VLM filter
  → ranked files
```

**Where quality leaks:** weak/missing captions · query-mode collisions · visual ANN ≠ identity · apparel+brand backdrop co-mention · cache without quality version · no deploy-time search SLO.

### 4.3 Face path today

- Model: InsightFace `buffalo_l`
- Providers default: `["CPUExecutionProvider"]`
- Jobs: durable `face_jobs` with `FOR UPDATE SKIP LOCKED` when `FACE_JOBS_ENABLED`
- Workers: Railway CPU replicas (~2 GB / 2 vCPU, sequential InsightFace lock)
- OCR: RapidOCR ONNX idle side-lane; `ocr_lane_enabled` defaults **false**; does not download on search request path

---

## 5. GPU needs (purchase brief)

### 5.1 Principle

| Workload | Compute home | Why |
|---|---|---|
| InsightFace / ArcFace | **External GPU (CUDA)** | Throughput + latency for people index and people search |
| Optional local VLM (Qwen3-VL stub exists; off) | **External GPU** (phase 2) | Only after face GPU is healthy |
| RapidOCR | CPU ONNX on idle workers | Geometry for brand; no GPU required |
| Whisper / ffmpeg | Railway indexer CPU | Already local; no GPU ask this quarter |
| Gemini embed/caption/VLM | Google API | Paid TPM — not a GPU box |
| Claude carousel | Anthropic API | Script quality |
| API + Postgres + Qdrant + Drive | Railway CPU | Fine without GPU |

### 5.2 Recommended starting SKU

| Item | Recommendation |
|---|---|
| **GPU class** | 1× NVIDIA **L4** or **T4**-class, 16–24 GB VRAM |
| **Providers** | RunPod · Modal · Vast.ai · GCP L4 · AWS `g4dn` / L4 |
| **Process** | Same app image or thin face-worker image; claim `face_jobs` from Postgres |
| **Providers env** | `INSIGHTFACE_PROVIDERS=["CUDAExecutionProvider","CPUExecutionProvider"]` |
| **Concurrency** | Start **1–2** parallel face jobs; raise only with measured VRAM headroom |
| **Storage** | **No** shared Drive volume on GPU box; re-fetch bytes / use short-lived cache |
| **Network** | Private-ish path to Postgres; monitor egress cost |
| **HA** | 1 GPU worker first; add second when backlog SLO fails |

### 5.3 What the GPU box must *not* host

- User-facing search/API (keep on Railway)
- Authoritative Postgres or Qdrant
- Long-lived Drive cache as source of truth
- Unbounded GGUF VLM serving that starves InsightFace

### 5.4 Phase-2 GPU (optional local judge)

Only if API VLM cost/latency becomes the limiter after structure is in place:

| Option | Notes |
|---|---|
| Qwen3-VL (code path `qwen_vlm_enabled`, currently false) | Needs GPU sidecar; HTTP to `qwen_vlm_base_url` |
| MiniCPM-V / Moondream GGUF | Possible on larger GPU; **yes/no on top-20 only**, never full corpus |
| Stay on Gemini VLM API | Often cheaper to start; GPU VLM is an optimization |

A mini-VLM that re-captions the whole image will **re-learn backdrop mistakes**. Prefer OCR geometry first; VLM only as shortlist yes/no.

### 5.5 Cost framing (three wallets)

1. **Platform (Railway)** — API, indexer, volume, data adjacency.  
2. **GPU (external)** — InsightFace first; optional local VLM later.  
3. **Models (APIs)** — Gemini / Claude / OpenRouter usage. Raise TPM only after interactive vs backfill isolation.

Spending only on APIs while faces stay CPU-bound does not unlock people-search throughput. Spending only on GPU while ranking stays embedding-primary does not fix apparel/brand precision.

---

## 6. Target architecture

### 6.1 Target topology

```text
Users → Frontends
     → API service          [Railway CPU]  search, auth, Range streaming
     → Indexer service      [Railway CPU + volume]  downloads, Whisper, ffmpeg, enqueue
     → Face GPU fleet       [external CUDA]  InsightFace; later optional VLM
     → Enrichment lanes     [CPU]  RapidOCR, objects — yield to faces
Shared:
     Postgres (SoR) · Qdrant (derived) · media lifecycle / object store
```

### 6.2 Search path we are moving toward

```text
Retrieve   visual + caption + object candidates (existing indexes)
    ↓
Fuse       caption/object-first for apparel/scene; visual ANN as recall floor
    ↓
Structure  apparel association · OCR torso/upper vs signage · hard-negatives
    ↓
Judge      budgeted VLM / caption LLM on top-N only
    ↓
Promote    only after golden eval beats production default
```

### 6.3 Isolation rules (non-negotiable)

- Do not change default production ranking until eval says so.
- OCR / experimental fusion must not Drive-download or commit the request DB session on the search path.
- GPU face service exposes health + claim semantics; API never blocks on InsightFace.
- Gemini interactive quota isolated from background caption/embed backfill.
- OCR lane and backfill default **off**; enable only for controlled enrich windows.

---

## 7. Implementation roadmap

| Phase | Work | Outcome | Compute |
|---|---|---|---|
| **0 — Stabilize** | API/indexer isolation; disk auto-clean; healthy `face_jobs` | Index finishes; UI stays live | Railway CPU |
| **1 — GPU faces** | External CUDA workers claim jobs; CUDA-first providers | People coverage & speed | **GPU SKU** |
| **2 — Structure** | OCR region quality; apparel association; object hard-negatives | Brand-on-garment ≠ backdrop | CPU ONNX |
| **3 — Rank policy** | Promote caption/structure fusion after A/B + golden set | Less lookalike junk in top-5 | No new GPU |
| **4 — Top-K judge** | Budgeted VLM rerank on top 20–50 hard queries | Industry-grade precision | API VLM or GPU VLM |
| **5 — SLO** | Golden + `caption_queries_500` on deploy; slice by query type | No silent regressions | Existing Gemini key |

### 7.1 Phase 1 acceptance (GPU faces)

- [ ] Face worker reports CUDA provider in use  
- [ ] `face_jobs` backlog drains under load without API latency spike  
- [ ] People-query precision@5 holds or improves on fixed set  
- [ ] Failover to CPU provider only if CUDA unavailable (alert)  
- [ ] No volume mount requirement for horizontal GPU scale  

### 7.2 Phase 2–3 acceptance (structure + rank)

- [ ] OCR spans carry usable `torso` / `upper` / `signage` regions (not ~all `other`)  
- [ ] Apparel-brand A/B: backdrop demoted vs production on hard queries  
- [ ] Object hard-negatives still pass (rowing / sandbag / ski-erg)  
- [ ] Production `/search` unchanged until promote decision  

### 7.3 Phase 4–5 acceptance (judge + SLO)

- [ ] VLM judge invoked only on shortlist; spend per query capped  
- [ ] `eval_search_quality.py` (+ 500-query set) runs on deploy or nightly  
- [ ] Separate scores: people · scene · apparel_brand · object · OCR/text  

---

## 8. How we implement “what companies do” — concrete mapping

| Industry practice | Our implementation move |
|---|---|
| ANN as recall only | Keep Qdrant visual ANN; demote fusion weight; strong visual floor for missing-caption fallback |
| Multi-signal retrieve | Caption ANN + lexical + object DB already hybrid; keep |
| OCR for text-in-image | RapidOCR on idle workers; persist spans; association uses region |
| Face graph / identity | InsightFace on **GPU**; face tags in person queries |
| Cross-encoder / VLM rerank | Caption LLM now; true pixel yes/no on top-K next |
| Offline eval + regression | Wire golden + caption_queries_500; make B8 a real gate |
| Service isolation | API ≠ indexer ≠ face GPU (same pattern as large media platforms) |

### Order that actually moves quality

1. GPU InsightFace (coverage + people latency)  
2. OCR geometry into apparel-brand association  
3. Promote structured ranking only with measured A/B  
4. Top-K VLM for residual lookalikes OCR cannot settle  
5. Deploy SLO so we stop whack-a-mole  

GGUF-first on Railway face workers is the romantic path. **CUDA faces + OCR-on-ONNX** match the measured failures and the existing stack.

---

## 9. Success metrics

| Metric | Signal |
|---|---|
| Face job latency / backlog | GPU workers drain queue without starving API |
| People precision@5 | Named faces before lookalikes |
| Apparel-brand precision@5 | On-garment over backdrop/signage |
| Object / action precision@5 | Hard negatives hold |
| VLM judge spend / query | Only top-K; no full-corpus VLM |
| Golden / 500-query on deploy | Alert or fail on regression |
| Caption/index completeness | Embeds + captions present for PROCESSED media |

---

## 10. Risks and mitigations

| Risk | Mitigation |
|---|---|
| GPU vendor lock / spot eviction | CUDA + CPU fallback providers; job lease/retry already designed |
| Egress cost GPU ↔ Postgres/Drive | Batch claims; short-lived image cache on GPU host |
| Search experiment harms prod | Isolated policy / endpoint; OCR lane off by default |
| OCR mis-region → wrong demotion | Require confidence + region; A/B before promote |
| VLM spend explosion | Hard top-K + concurrency cap; starve if face backlog |
| Quota 429s during backfill | Interactive vs backfill isolation (existing quality blocker B5) |

---

## 11. Bottom line

Other companies do **not** ship “embeddings = answer.” They ship **retrieve → structure → top-K judge**, with faces and OCR as first-class signals.

We are moving the same way:

- **GPU** for InsightFace (correct purchase; Railway CPU is not).  
- **Railway** for API, indexer, storage adjacency.  
- **Structure** (OCR, objects, captions) before trusting rank.  
- **Judge** only on a shortlist.  
- **Promote** only with eval — production search stays safe until then.

---

## Appendix A — Config knobs (reference)

```text
# Face / GPU
INSIGHTFACE_PROVIDERS=["CUDAExecutionProvider","CPUExecutionProvider"]
FACE_JOBS_ENABLED=true
RUN_FACE_WORKER=true
FACE_WORKER_CONCURRENCY=1   # raise carefully on GPU

# API
RUN_INDEXER=false
RUN_FACE_WORKER=false

# Indexer
RUN_INDEXER=true
FACE_JOBS_ENABLED=true

# OCR (opt-in)
ocr_lane_enabled=false          # runtime setting; default off
ocr_backfill_enabled=false

# Local VLM (later)
QWEN_VLM_ENABLED=false
QWEN_VLM_BASE_URL=http://...
```

## Appendix B — Suggested reading in-repo

- `docs/indexer-service.md` — API / indexer / face-worker split  
- `docs/quality-blockers-and-subscriptions.md` — B1–B8 quality ladder  
- `docs/full-system-architecture-and-risks.md` — full topology and risks  
- `backend/scripts/eval_search_quality.py` — golden judge  
- `datasets/caption_queries_500.csv` — broader eval substrate (wire next)
