# Quality blockers and funding

**System:** DriveFaceIndexer + Carousel Studio  
**Purpose:** one page for what actually blocks quality, what to drive next, and whether new subscriptions are required.  
**Evidence:** production incidents (ENOSPC, overlapping video workers, Whisper-empty transcripts), current code (`backend/.env.example`, carousel LLM routing, video/Whisper/InsightFace pipelines), and the 14 Aug 2026 architecture register.  
**Audience:** product + ops decisions. Architecture dump lives in `docs/full-system-architecture-and-risks.md`.

---

## 1. What “quality” means here

Quality is not one model. It is five product outcomes, each with a different saturator:

| Outcome | Quality means | Saturator if it fails |
|---|---|---|
| Library permanence | Indexed work stays; Drive folder changes do not wipe PROCESSED rows | Indexer policy + disk |
| Search | The right faces, scenes, and spoken moments rank first | Completeness of faces, captions, embeddings, transcripts |
| Video index | Transcript + faces + frames exist for every processed video | CPU (ffmpeg / InsightFace / Whisper) + disk + claim locks |
| Carousel script | Themes, topics, hooks, and slide copy people would post | LLM quality + verbatim transcript timestamps |
| Carousel images | Centered, front-facing guest; frame matches the hook | Local face scores first; vision only when heuristics miss |

If disk, claims, or quota collapse, model spend cannot raise quality. Pay for models only after the index can finish and stay on disk.

---

## 2. Blockers (do these before buying more models)

Ordered by **blocks live quality today**, not theoretical architecture rank.

| # | Blocker | Why quality dies | Drive this, not that |
|---|---|---|---|
| B1 | Leftover Drive downloads can refill `/app/data` | ENOSPC stops new index, carousel previews, and backups. Manual cleanup already recovered ~41 GiB; **auto-unlink on success and ERROR** is the required control. | Ship auto-clean. Keep uploads and YouTube. Do not buy more volume until this lands. |
| B2 | Overlapping video workers + 900s stall watchdog | Proof video logged pipeline success then ERROR; Media/segments committed with **empty transcript and no VLM cues**. Duplicate ffmpeg/LLM burns quota for zero quality. | One indexer replica until durable leases; fencing token on finalize. |
| B3 | Whisper / transcript gaps | Hooks must be exact transcript sentences at the right timestamp. Empty cues → heuristic fallbacks and unusable carousels. | Confirm faster-whisper is loaded on the indexer image; backfill empty-cue videos. Local CPU — **no new subscription**. |
| B4 | API and indexer still share one Railway process | Search, Range preview, and InsightFace compete for CPU, RAM, and DB pool. Interactive carousel feels “broken” while a video indexes. | Split `dfi-backend` (`RUN_INDEXER=false`) vs `dfi-indexer` (same image). Railway replica cost, not a SaaS. |
| B5 | Gemini quota shared across captions, embeds, VLM, and carousel | Background backfill 429s starve interactive generate. Weak fallbacks get cached and reused. | Workload budgets: pause caption/embed backfill on 429; reserve interactive quota. Raise paid Gemini **only after** isolation. |
| B6 | Long carousel stages are still synchronous | Proxy timeout / retry doubles spend; user sees failure while the provider still runs. | Persist per-stage jobs + idempotency. Engineering, not a new vendor. |
| B7 | Weak carousel generations can poison cache | Cache key is provider+model+transcript, not prompt/policy version. A bad first theme poisons every hook. | Keep Claude as script default; regenerate must force new cache id. Do not cheaper-cache over good Sonnet output. |
| B8 | Search quality has no regression gate | `eval_search_quality.py` exists but is not a live SLO. Relevance can drift after embed/caption changes. | Run golden set after indexer deploys. Uses existing Gemini key. |

Architecture items that **are not quality blockers this quarter:** Qwen GPU sidecar (Railway has no GPU), Cloud Vision, SerpAPI, Cohesivity LinkedIn, duplicating `/test` features onto `/carousel`.

---

## 3. What should drive quality (the ladder)

Do the next rung only when the previous one is green.

### Rung 1 — Finish the index (completeness)

Nothing downstream is trustworthy until a video has: Media row, non-empty transcript cues, face rows, and frame samples.

- Keep **InsightFace** local (CPU). Quality of “who is in the shot” depends on coverage, not a SaaS face API.
- Keep **ffmpeg** local. Do not replace with a hosted transcode product.
- Keep **faster-whisper** local (`whisper_model_size=base` today). Upgrade size (small/medium) only if empty/garbled cues persist after the stall/overlap fix.
- **Gemini embeddings + captions** remain the search backbone. File Search is already retired (`GEMINI_FILE_SEARCH_DISABLED=true`); do not buy a second RAG product.

### Rung 2 — Grounded carousel text (the product)

Script quality is the carousel. Layout cannot save a bad hook.

- **Claude direct (Sonnet 4.5)** is the default for themes / topics / hooks. This is the quality bar.
- Use cheaper Draft models only for volume exploration, then polish once on Sonnet.
- Hooks must be substrings of the transcript at the given timestamp. That is a pipeline rule, not a model subscription.

### Rung 3 — Frames that look intentional

- Local front-face / center scores already exist (`carousel_frame_select`). Run that first.
- Vision (Gemini or OpenRouter vision) is a **tie-breaker** when heuristics miss (profile, back of head, edge crop). Do not pay for vision on every frame.

### Rung 4 — Search relevance loop

- Golden queries in `backend/scripts/eval_search_quality.py`.
- Judge with the Gemini key you already pay for.
- Track precision on people vs scene vs spoken-text queries separately.

---

## 4. Subscriptions and accounts

**Legend**

- **Keep / already in stack** — required for the product as designed.
- **Fund credits** — usage billing, not a named monthly “subscription” unless you want a committed spend.
- **Do not buy** — no quality return this quarter.

### Keep (required)

| Account | What it powers | Subscription? |
|---|---|---|
| Railway | API, carousel, volume, Postgres/Qdrant hosting | **Yes, keep.** Indexer split may add one replica. Prefer that over a bigger GPU box. |
| Google Cloud project | Drive API, OAuth, Picker (browser key + HTTP referrers) | **Project + APIs, not a Face product.** Stay on current quotas unless Drive listing 403s. |
| Gemini API (`GEMINI_API_KEY`) | Image/video embeddings, captions, VLM, search eval | **Usage billing. Keep paid quota.** This is the search/index quality floor. Watch TPM/429s before upgrading the tier. |
| Anthropic (`ANTHROPIC_API_KEY`) | Default carousel themes / extract / polish | **Usage billing. Keep.** Do not drop this for “Gemini is cheaper” — cheap scripts are the quality regression. |

### Fund (quality + failover — not a new vendor category)

| Account | What it unlocks | Do you need a subscription? |
|---|---|---|
| OpenRouter (`OPENROUTER_API_KEY`) | Model picker on `/test`, failover when Gemini/Anthropic 429, optional GPT / Llama / Gemini-via-OR | **Credits, not a mandatory monthly plan.** Fund enough for carousel generate + a small failover buffer. Image-gen models can wait. |
| Gemini paid tier bump | Higher embed/caption/VLM concurrency without starving carousel | **Only after** backfill is isolated from interactive quota (B5). Extra spend without isolation just fills 429s faster. |

OpenRouter is the **only new spend that directly lifts carousel quality and uptime**. It does not replace Gemini embeddings or Claude-as-default.

### Do not buy this quarter

| Offer | Why skip |
|---|---|
| Qwen3-VL / any GPU VLM host | Code exists (`QWEN_VLM_ENABLED=false`). Railway has no GPU. Local caption fallback is already Gemini. |
| Google Cloud Vision Web Detection | Repo notes it is weak for people ID. |
| SerpAPI Google Lens | Optional fallback behind Apify. Skip unless reverse-image identity is a launch requirement. |
| Apify Google Lens | Useful for **review-queue identity**, not carousel scripts or search embeddings. Buy only if people naming is blocking launch. |
| Cohesivity / Exa LinkedIn | Enrichment, not index or carousel quality. |
| Hosted Whisper / AssemblyAI / Deepgram | Local faster-whisper is already wired. Pay a speech API only if CPU Whisper stays empty after B2/B3. |
| Second vector DB / Gemini File Search revival | File Search is retired. Qdrant is the index. |
| Extra Railway volume size | Treats the symptom of B1. Auto-clean first. |

### Cookies and keys that are not subscriptions

- **YouTube Netscape cookies** (`YTDLP_COOKIES_FILE`) — operational file, required for yt-dlp on Railway. Refresh when downloads 403. Not a paid plan.
- **Google Picker browser API key** — must include `https://dfi-carousel-production.up.railway.app/*` on HTTP referrers. Config, not spend.

---

## 5. Decision

1. **No new SaaS vendor is required** to unstick quality. The stack is Railway + Gemini + Claude + Drive.
2. **Yes, fund OpenRouter credits** so `/test` model choice and vendor failover are real. Without credits, the picker is a dead control and Gemini/Claude outages ship heuristic fallbacks.
3. **Yes, keep Anthropic billed** as the script quality default. Cancelling it to “save” money will drop hook quality immediately.
4. **Maybe raise Gemini quota** after interactive vs backfill isolation. Do not raise it first.
5. **Skip GPU, Vision, SerpAPI, Apify, Exa** unless people reverse-search becomes a launch gate.

### Suggested order of work (quality, not architecture completeness)

1. Auto-unlink leftover Drive caches on success/ERROR (B1).
2. Single-owner video execution + stop stall-watchdog false ERROR (B2).
3. Whisper present + backfill empty transcripts (B3).
4. API / indexer service split (B4).
5. Quota isolation + OpenRouter credits for generate/failover (B5 + fund).
6. Golden search eval on deploy (B8).
7. Only then: Whisper model size bump, vision tie-breakers, Apify.

Until rungs 1–3 are green, extra model subscriptions will not show up as better carousels or better search. They will show up as more 429s and more cached junk.
