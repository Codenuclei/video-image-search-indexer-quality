/**
 * Local-only mock API for carousel studio demos.
 * Enabled when NEXT_PUBLIC_API_URL=/api/test (or USE_TEST_API=1 points the client here).
 * Does not call production backends.
 */
import { NextRequest, NextResponse } from "next/server";
import {
  MOCK_HOOKS,
  MOCK_THEMES,
  MOCK_TOPICS,
  MOCK_TRANSCRIPT_FRAMES,
  MOCK_VIDEO,
  MOCK_VIDEO_2,
  mockExtract,
  mockExtractHooks,
  mockGenerate,
} from "@/lib/test-mock-data";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ path: string[] }> };

const MOCK_LLM_PROVIDERS = [
  { id: "claude", label: "Claude (direct)" },
  { id: "openrouter", label: "OpenRouter" },
  { id: "gemini", label: "Gemini" },
  { id: "auto", label: "Auto" },
];

const MOCK_LLM_MODELS = [
  { id: "claude-sonnet-4-5-20250929", label: "Claude Sonnet 4.5 (direct)", provider: "claude" },
  { id: "claude-sonnet-4-20250514", label: "Claude Sonnet 4 (direct)", provider: "claude" },
  { id: "claude-opus-4-20250514", label: "Claude Opus 4 (direct)", provider: "claude" },
  { id: "anthropic/claude-sonnet-4", label: "Claude Sonnet 4", provider: "openrouter" },
  { id: "anthropic/claude-sonnet-4.5", label: "Claude Sonnet 4.5", provider: "openrouter" },
  { id: "google/gemini-2.5-pro", label: "Gemini 2.5 Pro", provider: "openrouter" },
  { id: "google/gemini-2.5-flash", label: "Gemini 2.5 Flash", provider: "openrouter" },
  { id: "openai/gpt-4.1", label: "GPT-4.1", provider: "openrouter" },
  { id: "openai/gpt-4.1-mini", label: "GPT-4.1 Mini", provider: "openrouter" },
  { id: "meta-llama/llama-4-maverick", label: "Llama 4 Maverick", provider: "openrouter" },
  { id: "gemini-2.5-pro", label: "Gemini 2.5 Pro", provider: "gemini" },
  { id: "gemini-2.5-flash", label: "Gemini 2.5 Flash", provider: "gemini" },
];

/** In-memory mock settings for NEXT_PUBLIC_TEST_USE_REAL_API=0. */
let mockSettings = {
  gemini_model: "gemini-2.0-flash",
  gemini_file_search_store_display_name: "test-store",
  auto_index_enabled: false,
  auto_index_interval_seconds: 300,
  reindex_errored_files: false,
  reindex_skipped_files: false,
  follow_shortcut_folders: false,
  experimental_manual_face_tag: false,
  gemini_file_search_search_enabled: false,
  search_parallel_variants_enabled: false,
  search_use_captions: true,
  search_rerank_enabled: false,
  go_indexer_enabled: false,
  carousel_llm_provider: "auto" as string,
  openrouter_model: "anthropic/claude-sonnet-4",
  openrouter_configured: true,
  carousel_llm_model_options: MOCK_LLM_MODELS,
  carousel_llm_providers: MOCK_LLM_PROVIDERS,
};

function json(data: unknown, status = 200) {
  return NextResponse.json(data, { status });
}

function pathKey(parts: string[]) {
  return parts.join("/");
}

async function readBody(req: NextRequest): Promise<Record<string, unknown>> {
  try {
    return (await req.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

async function handle(req: NextRequest, ctx: Ctx) {
  const { path: parts } = await ctx.params;
  const path = pathKey(parts ?? []);
  const method = req.method.toUpperCase();
  const url = new URL(req.url);

  // Health / ping
  if (path === "health" || path === "test/health") {
    return json({ ok: true, mode: "test", message: "Carousel test API" });
  }

  if (path === "persons" && method === "GET") {
    return json([
      {
        id: 1,
        name: "Alex Rivera",
        role: "student",
        representative_face_id: null,
        occurrence_count: 12,
        created_at: "2026-07-01T00:00:00Z",
      },
    ]);
  }

  if (path === "youtube/videos" && method === "GET") {
    return json([
      {
        id: "yt-test-001",
        name: "Indexed YouTube — Campus tour highlights",
        mime_type: "video/mp4",
        path: "/youtube/campus-tour.mp4",
        status: "ready",
        size: 35_000_000,
        modified_time: "2026-08-09T10:00:00Z",
        last_synced_at: "2026-08-09T10:05:00Z",
        error_message: null,
        source: "youtube",
      },
    ]);
  }

  if (path === "youtube/videos" && method === "POST") {
    const body = await readBody(req);
    const urls = Array.isArray(body.urls) ? (body.urls as string[]) : [];
    const registered = urls.map((u, i) => {
      const id = `yt-indexed-${Date.now()}-${i}`;
      return {
        drive_file_id: id,
        name: `YouTube (test) — ${u.slice(0, 48)}`,
        youtube_video_id: "dQw4w9WgXcQ",
        linked_to_drive: false,
        download_queued: false,
        message: "Mock-indexed locally (test API). Ready for studio.",
      };
    });
    // Also surface in recent list via a static id when empty
    if (!registered.length) {
      registered.push({
        drive_file_id: "yt-indexed-demo",
        name: "YouTube (test) — demo link",
        youtube_video_id: "dQw4w9WgXcQ",
        linked_to_drive: false,
        download_queued: false,
        message: "Mock-indexed locally (test API).",
      });
    }
    return json({
      ok: true,
      registered,
      index_scheduled: true,
    });
  }

  if (path === "search/carousel/recent-videos" && method === "GET") {
    return json({ items: [MOCK_VIDEO, MOCK_VIDEO_2], captioned_only: true });
  }

  if (path === "search/carousel/videos" && method === "GET") {
    const q = (url.searchParams.get("q") || "").toLowerCase();
    let items = [MOCK_VIDEO, MOCK_VIDEO_2];
    if (q) items = items.filter((v) => v.name.toLowerCase().includes(q));
    return json({
      items,
      q: q || null,
      captioned_only: true,
      limit: 20,
      offset: 0,
      has_more: false,
    });
  }

  if (path === "search/carousel/pipeline/themes" && method === "POST") {
    const body = await readBody(req);
    const driveFileId = String(body.drive_file_id || MOCK_VIDEO.id);
    return json({
      source: "test",
      drive_file_id: driveFileId,
      name: MOCK_VIDEO.name,
      harmonized: true,
      cue_count: MOCK_VIDEO.cue_count,
      themes: MOCK_THEMES,
      cache_hit: !body.force && !body.generate,
      generated: Boolean(body.generate || body.force),
      save_id: 8001,
      person_found: true,
    });
  }

  if (path === "search/carousel/pipeline/extract" && method === "POST") {
    const body = await readBody(req);
    const driveFileId = String(body.drive_file_id || MOCK_VIDEO.id);
    // Mirror real backend: force bypasses cache; generate alone still cache-hits.
    const live = Boolean(body.force);
    return json({
      ...mockExtract(driveFileId, { include_hooks: Boolean(body.include_hooks) }),
      cache_hit: !live,
      generated: live,
    });
  }

  if (path === "search/carousel/pipeline/extract/hooks" && method === "POST") {
    const body = await readBody(req);
    const driveFileId = String(body.drive_file_id || MOCK_VIDEO.id);
    const topicTexts = Array.isArray(body.topics)
      ? (body.topics as { text?: string }[]).map((t) => String(t.text || "")).filter(Boolean)
      : [];
    return json(mockExtractHooks(driveFileId, topicTexts));
  }

  if (path === "search/carousel/pipeline/intent" && method === "POST") {
    return json({
      intent: "Reassure families that admissions is transparent and community-led.",
      intent_score: 0.86,
      intent_source: "test",
    });
  }

  if (path === "search/carousel/pipeline/generate" && method === "POST") {
    const body = await readBody(req);
    const driveFileId = String(body.drive_file_id || MOCK_VIDEO.id);
    const hooks = Array.isArray(body.hooks)
      ? (body.hooks as { text?: string }[]).map((h) => h.text || "").filter(Boolean)
      : [];
    const topics = Array.isArray(body.topics)
      ? (body.topics as { text?: string }[]).map((t) => t.text || "").filter(Boolean)
      : [];
    const out = mockGenerate(driveFileId, hooks, topics);
    // Text-first generate: strip frames so Select images is a real next step.
    if (body.select_images !== true) {
      const strip = (c: (typeof out.carousels)[number]) => ({
        ...c,
        images_ready: false,
        slides: (c.slides ?? []).map((s) => ({
          ...s,
          preview_url: null,
          images_ready: false,
        })),
      });
      const carousels = (out.carousels ?? []).map(strip);
      return json({
        ...out,
        carousels,
        images_ready: false,
        cache_hit: !body.force,
        generated: Boolean(body.force),
        layouts: {
          single_1: { layout_mode: "single_1", carousels },
          split_2: { layout_mode: "split_2", carousels },
        },
      });
    }
    return json({
      ...out,
      cache_hit: !body.force,
      generated: Boolean(body.force),
    });
  }

  if (path === "search/carousel/pipeline/select-images" && method === "POST") {
    const body = await readBody(req);
    const driveFileId = String(body.drive_file_id || MOCK_VIDEO.id);
    const carouselsIn = Array.isArray(body.carousels) ? body.carousels : [];
    if (carouselsIn.length) {
      const carousels = carouselsIn.map((c: Record<string, unknown>, ci: number) => {
        const slides = Array.isArray(c.slides) ? c.slides : [];
        return {
          ...c,
          images_ready: true,
          slides: slides.map((s: Record<string, unknown>, i: number) => ({
            ...s,
            preview_url:
              typeof s.preview_url === "string" && s.preview_url
                ? s.preview_url
                : `https://picsum.photos/seed/sel${ci}${i}/720/1280`,
            images_ready: true,
          })),
        };
      });
      return json({
        ...mockGenerate(driveFileId, [], []),
        carousels,
        images_ready: true,
        cache_hit: false,
        generated: true,
        layouts: {
          single_1: { layout_mode: "single_1", carousels },
          split_2: { layout_mode: "split_2", carousels },
        },
      });
    }
    return json({
      ...mockGenerate(driveFileId, [], []),
      cache_hit: false,
      generated: true,
    });
  }

  if (path === "search/carousel/pipeline/feedback" && method === "GET") {
    return json({
      drive_file_id: url.searchParams.get("drive_file_id") || MOCK_VIDEO.id,
      items: [],
    });
  }

  if (path === "search/carousel/pipeline/feedback" && method === "PUT") {
    const body = await readBody(req);
    return json({
      ok: true,
      item: {
        id: Math.floor(Math.random() * 10_000),
        drive_file_id: body.drive_file_id,
        target_kind: body.target_kind,
        target_key: body.target_key,
        target_label: body.target_label ?? null,
        rating: body.rating ?? null,
        comment: body.comment ?? null,
        updated_at: new Date().toISOString(),
      },
    });
  }

  if (path === "search/carousel/pipeline/references" && method === "GET") {
    return json({
      drive_file_id: url.searchParams.get("drive_file_id") || MOCK_VIDEO.id,
      items: [],
    });
  }

  if (path === "search/carousel/pipeline/references" && method === "POST") {
    const body = await readBody(req);
    return json({
      ok: true,
      item: {
        id: Math.floor(Math.random() * 10_000),
        drive_file_id: body.drive_file_id,
        target_kind: body.target_kind,
        target_key: body.target_key,
        target_label: body.target_label ?? null,
        ref_kind: body.ref_kind,
        image_url: body.image_url ?? null,
        frame_ts: body.frame_ts ?? null,
        copy_text: body.copy_text ?? null,
        note: body.note ?? null,
        updated_at: new Date().toISOString(),
      },
    });
  }

  if (path.startsWith("search/carousel/pipeline/references/") && method === "DELETE") {
    const id = Number(path.split("/").pop());
    return json({ ok: true, id });
  }

  if (path === "search/carousel/pipeline/references/upload-image" && method === "POST") {
    return json({
      ok: true,
      url: `https://picsum.photos/seed/upload${Date.now()}/720/1280`,
      name: "upload.jpg",
      size: 12_000,
    });
  }

  if (path === "search/carousel/pipeline/prerun" && method === "POST") {
    return json({
      count: 1,
      ok_count: 1,
      force: false,
      items: [
        {
          drive_file_id: MOCK_VIDEO.id,
          ok: true,
          themes_cache_hit: true,
          themes_generated: false,
          theme_count: MOCK_THEMES.length,
          extract_cache_hit: true,
          extract_generated: false,
          hook_count: MOCK_HOOKS.length,
          topic_count: MOCK_TOPICS.length,
        },
      ],
    });
  }

  if (path === "search/carousel/upload" && method === "POST") {
    return json({
      drive_file_id: `upload-${Date.now()}`,
      name: "Uploaded demo video.mp4",
      status: "ready",
      size: 10_000_000,
      queued: false,
      message: "Mock upload accepted (test API).",
    });
  }

  if (path === "search/carousel/pipeline/carousel" && method === "GET") {
    const driveFileId = url.searchParams.get("drive_file_id") || MOCK_VIDEO.id;
    const gen = mockGenerate(driveFileId, [], []);
    return json({
      id: 7001,
      status: "ready",
      layout_mode: "single_1",
      copy_version: 1,
      slides: gen.slides,
      carousels: gen.carousels,
      layouts: gen.layouts,
      title: gen.title,
    });
  }

  if (path === "search/carousel/pipeline/status" && method === "GET") {
    return json({
      drive_file_id: url.searchParams.get("drive_file_id") || MOCK_VIDEO.id,
      status: "idle",
      locked: false,
      ready_artifact_id: null,
    });
  }

  if (path === "search/carousel/pipeline/carousel/copy" && method === "POST") {
    return json({ id: 7001, copy_version: 2, layout_mode: "single_1" });
  }

  if (path === "search/carousel/pipeline/carousel/slide/regenerate" && method === "POST") {
    const body = await readBody(req);
    const gen = mockGenerate(String(body.drive_file_id || MOCK_VIDEO.id), [], []);
    return json({
      id: 7001,
      copy_version: 2,
      slide: gen.slides[Number(body.slide_index) || 0] || gen.slides[0],
    });
  }

  if (path === "search/carousel/pipeline/saves" && method === "GET") {
    const kind = url.searchParams.get("kind") || "topics_hooks";
    if (kind === "themes") {
      return json({
        items: [
          {
            id: 8001,
            drive_file_id: MOCK_VIDEO.id,
            kind: "themes",
            theme_key: "all",
            label: "Demo themes",
            created_at: "2026-08-10T12:00:00Z",
            theme_count: MOCK_THEMES.length,
            status: "ready",
          },
        ],
      });
    }
    if (kind === "carousel") {
      return json({
        items: [
          {
            id: 7001,
            drive_file_id: MOCK_VIDEO.id,
            kind: "carousel",
            theme_key: "all",
            label: "Demo carousel save",
            created_at: "2026-08-11T12:00:00Z",
            status: "ready",
            layout_mode: "single_1",
          },
        ],
      });
    }
    return json({
      items: [
        {
          id: 9001,
          drive_file_id: MOCK_VIDEO.id,
          kind: "topics_hooks",
          theme_key: "all",
          label: "Demo topics & hooks",
          created_at: "2026-08-10T14:00:00Z",
          hook_count: MOCK_HOOKS.length,
          topic_count: MOCK_TOPICS.length,
          status: "ready",
          source: "autosave",
        },
      ],
    });
  }

  if (path.startsWith("search/carousel/pipeline/saves/") && method === "GET") {
    const id = Number(path.split("/").pop());
    const extract = mockExtract(MOCK_VIDEO.id);
    return json({
      id,
      drive_file_id: MOCK_VIDEO.id,
      kind: "topics_hooks",
      theme_key: "all",
      label: "Demo save",
      created_at: "2026-08-10T14:00:00Z",
      source: "autosave",
      payload: {
        ...extract,
        themes: MOCK_THEMES,
        selected_hooks: [],
        selected_topics: [],
      },
    });
  }

  if (path === "search/carousel/pipeline/saves" && method === "POST") {
    return json({ id: 9002, created_at: new Date().toISOString() });
  }

  if (path === "search/carousel/pipeline/shuffle" && method === "POST") {
    return json({
      selected_hooks: MOCK_HOOKS.slice(0, 2).map((h) => h.text),
      selected_topics: MOCK_TOPICS.slice(0, 2).map((t) => t.text),
      hooks: MOCK_HOOKS,
      topics: MOCK_TOPICS,
    });
  }

  if (path === "search/carousel/pipeline/transcript-frames" && method === "GET") {
    return json({
      drive_file_id: url.searchParams.get("drive_file_id") || MOCK_VIDEO.id,
      items: MOCK_TRANSCRIPT_FRAMES,
    });
  }

  if (path === "settings" && method === "GET") {
    return json({ ...mockSettings });
  }

  if (path === "settings" && method === "PUT") {
    const body = await readBody(req);
    if (typeof body.carousel_llm_provider === "string") {
      mockSettings = { ...mockSettings, carousel_llm_provider: body.carousel_llm_provider };
    }
    if (typeof body.openrouter_model === "string") {
      mockSettings = { ...mockSettings, openrouter_model: body.openrouter_model };
    }
    return json({ ...mockSettings });
  }

  if (path === "settings/carousel-llm-models" && method === "GET") {
    return json({
      models: MOCK_LLM_MODELS,
      providers: MOCK_LLM_PROVIDERS,
      carousel_llm_model_options: MOCK_LLM_MODELS,
      carousel_llm_providers: MOCK_LLM_PROVIDERS,
      openrouter_configured: mockSettings.openrouter_configured,
      current: {
        carousel_llm_provider: mockSettings.carousel_llm_provider,
        openrouter_model: mockSettings.openrouter_model,
      },
    });
  }

  // Drive / media stubs — return opaque placeholders
  if (path === "api/session" && method === "GET") {
    return json({ connected: false });
  }
  if (path === "api/drive-token" && method === "GET") {
    return json({ accessToken: "mock", apiKey: "", appId: null }, 401);
  }
  if (path === "api/save-folder" && method === "POST") {
    return json({ ok: true, folder: { id: "mock-folder", name: "Mock Folder" } });
  }
  if (path === "api/logout" && method === "POST") {
    return json({ ok: true });
  }
  if (path === "drive/sync" && method === "POST") {
    return json({ ok: true, scheduled: true });
  }
  if (path === "index/folders" && method === "GET") {
    return json({ folders: [], total: 0 });
  }
  if (path === "drive/files/page" && method === "GET") {
    return json({ items: [], total: 0, offset: 0, limit: 60 });
  }
  if (path === "search/carousel/prioritize" && method === "POST") {
    return json({
      ok: true,
      queued: 0,
      message: "Mock prioritize (test API).",
      items: [],
    });
  }

  if (path.startsWith("drive/files/") && (method === "GET" || method === "HEAD")) {
    return new NextResponse("test-video-placeholder", {
      status: 200,
      headers: { "Content-Type": "video/mp4" },
    });
  }

  if (path.startsWith("media/") && method === "GET") {
    // Redirect to a picsum frame so <img> tags work in preview
    const seed = path.length % 50;
    return NextResponse.redirect(`https://picsum.photos/seed/media${seed}/720/1280`);
  }

  return json(
    {
      detail: `Test API has no handler for ${method} /${path}`,
      hint: "Point NEXT_PUBLIC_API_URL=/api/test for local demos.",
    },
    404
  );
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
export const OPTIONS = handle;
export const HEAD = handle;
