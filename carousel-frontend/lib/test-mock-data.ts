/**
 * Rich mock payloads for local demo when NEXT_PUBLIC_USE_TEST_API=1.
 * Themes stay backend-only; UI consumes topics/hooks/carousels/preview frames.
 */

const FRAME = (seed: number, w = 720, h = 1280) =>
  `https://picsum.photos/seed/carousel${seed}/${w}/${h}`;

export const MOCK_VIDEO = {
  id: "test-video-demo-001",
  name: "Admissions Q&A — Spring Open House",
  mime_type: "video/mp4",
  path: "/library/admissions-qa.mp4",
  size: 48_000_000,
  modified_time: "2026-08-01T12:00:00Z",
  last_synced_at: "2026-08-10T09:00:00Z",
  created_at: "2026-07-20T10:00:00Z",
  status: "ready",
  has_captions: true,
  cue_count: 186,
};

export const MOCK_VIDEO_2 = {
  id: "test-video-demo-002",
  name: "Founder Story — Building in Public",
  mime_type: "video/mp4",
  path: "/library/founder-story.mp4",
  size: 62_000_000,
  modified_time: "2026-08-05T15:00:00Z",
  last_synced_at: "2026-08-11T11:00:00Z",
  created_at: "2026-08-01T08:00:00Z",
  status: "ready",
  has_captions: true,
  cue_count: 240,
};

export const MOCK_THEMES = [
  {
    theme_id: "theme-admissions",
    title: "Admissions journey",
    start_sec: 12,
    end_sec: 420,
    summary: "How applicants move from interest to enrolled student.",
    harmonized: true,
  },
  {
    theme_id: "theme-community",
    title: "Campus community",
    start_sec: 430,
    end_sec: 780,
    summary: "Peer support, clubs, and belonging on campus.",
    harmonized: true,
  },
  {
    theme_id: "theme-outcomes",
    title: "Outcomes & careers",
    start_sec: 790,
    end_sec: 1100,
    summary: "Internships, first jobs, and alumni networks.",
    harmonized: true,
  },
];

export const MOCK_HOOKS = [
  {
    id: "hook-1",
    text: "Most families think admissions is a black box — here is what actually happens after you hit submit.",
    start_sec: 45,
    end_sec: 62,
    verbatim: true,
    theme_id: "theme-admissions",
    topic_id: "topic-1",
    topic_text: "What happens after you apply",
  },
  {
    id: "hook-2",
    text: "We do not rank students on a single score. We look for fit across academics, voice, and grit.",
    start_sec: 118,
    end_sec: 140,
    verbatim: true,
    theme_id: "theme-admissions",
    topic_id: "topic-1",
    topic_text: "What happens after you apply",
  },
  {
    id: "hook-3",
    text: "The first friend you make in orientation week often becomes your co-founder later.",
    start_sec: 460,
    end_sec: 478,
    verbatim: true,
    theme_id: "theme-community",
    topic_id: "topic-2",
    topic_text: "Finding your people",
  },
  {
    id: "hook-4",
    text: "Belonging is not a poster on the wall — it is someone who notices when you skip lunch.",
    start_sec: 520,
    end_sec: 540,
    verbatim: true,
    theme_id: "theme-community",
    topic_id: "topic-2",
    topic_text: "Finding your people",
  },
  {
    id: "hook-5",
    text: "Eighty percent of our seniors already have an offer before graduation week.",
    start_sec: 820,
    end_sec: 838,
    verbatim: true,
    theme_id: "theme-outcomes",
    topic_id: "topic-3",
    topic_text: "From classroom to career",
  },
  {
    id: "hook-6",
    text: "Alumni do not disappear — they show up for mock interviews at 9pm on Zoom.",
    start_sec: 900,
    end_sec: 918,
    verbatim: true,
    theme_id: "theme-outcomes",
    topic_id: "topic-3",
    topic_text: "From classroom to career",
  },
];

export const MOCK_TOPICS = [
  {
    id: "topic-1",
    text: "What happens after you apply",
    start_sec: 40,
    end_sec: 200,
    explanation: "Timeline from application to decision day.",
    theme_id: "theme-admissions",
    has_subtopics: false,
  },
  {
    id: "topic-2",
    text: "Finding your people",
    start_sec: 450,
    end_sec: 620,
    explanation: "Clubs, mentors, and peer networks.",
    theme_id: "theme-community",
    has_subtopics: false,
  },
  {
    id: "topic-3",
    text: "From classroom to career",
    start_sec: 800,
    end_sec: 1050,
    explanation: "Internships, offers, and alumni support.",
    theme_id: "theme-outcomes",
    has_subtopics: false,
  },
  {
    id: "topic-4",
    text: "Money & aid myths",
    start_sec: 210,
    end_sec: 320,
    explanation: "Need-blind myths and how aid packages work.",
    theme_id: "theme-admissions",
    has_subtopics: false,
  },
];

export const MOCK_TOPIC_TREE = MOCK_TOPICS.map((t) => ({
  id: t.id,
  text: t.text,
  start_sec: t.start_sec,
  end_sec: t.end_sec,
  explanation: t.explanation,
  theme_id: t.theme_id,
  subtopics: [] as [],
  hooks: MOCK_HOOKS.filter((h) => h.topic_id === t.id),
}));

export function mockExtract(driveFileId: string) {
  return {
    drive_file_id: driveFileId,
    theme_ids: MOCK_THEMES.map((t) => t.theme_id),
    hooks: MOCK_HOOKS,
    topics: MOCK_TOPICS,
    topic_tree: MOCK_TOPIC_TREE,
    save_id: 9001,
    previews: MOCK_HOOKS.slice(0, 3).map((h) => ({
      start_sec: h.start_sec,
      end_sec: h.end_sec,
      text: h.text,
      label: "Hook",
      theme_id: h.theme_id,
      theme_title: MOCK_THEMES.find((t) => t.theme_id === h.theme_id)?.title,
    })),
    intent: "Reassure families that admissions is transparent and community-led.",
    intent_score: 0.86,
    intent_source: "test",
    verbatim: true,
    hooks_english: true,
    topics_english: true,
    any_translated: false,
    cache_hit: true,
    generated: false,
  };
}

function slide(
  index: number,
  hook: string,
  ts: number,
  seed: number,
  caption?: string
) {
  return {
    index,
    hook_line: hook,
    transcript_text: hook,
    caption: caption ?? hook.slice(0, 80),
    drive_file_id: MOCK_VIDEO.id,
    name: MOCK_VIDEO.name,
    timestamp_sec: ts,
    end_timestamp_sec: ts + 8,
    snippet: hook,
    moment_index: index,
    frame_ts: ts + 1,
    frame_source: "ai" as const,
    preview_url: FRAME(seed + index),
    instagram_ready: true,
    images_ready: true,
    frames_prewarmed: true,
    focal_x: 0.5,
    focal_y: 0.42,
  };
}

export function mockGenerate(driveFileId: string, hooks: string[], topics: string[]) {
  const pickedHooks = hooks.length
    ? MOCK_HOOKS.filter((h) => hooks.includes(h.text))
    : MOCK_HOOKS.slice(0, 2);
  const useHooks = pickedHooks.length ? pickedHooks : MOCK_HOOKS.slice(0, 2);
  const carousels = useHooks.map((h, ci) => {
    const slides = [
      slide(0, h.text, h.start_sec, 10 + ci * 10, "Hook"),
      slide(
        1,
        topics[0] || MOCK_TOPICS[0].text,
        h.start_sec + 20,
        11 + ci * 10,
        "Context"
      ),
      slide(2, "What this means for you", h.start_sec + 40, 12 + ci * 10, "Takeaway"),
      slide(3, "Save this for later →", h.start_sec + 55, 13 + ci * 10, "CTA"),
    ];
    return {
      id: `carousel-${h.id}`,
      kind: "hook" as const,
      title: completeTitle(h.text),
      topic_labels: topics.length ? topics : [MOCK_TOPICS[0].text],
      slide_count: slides.length,
      slides,
      hooks: [h.text],
      topics: topics.length ? topics : [MOCK_TOPICS[0].text],
      images_ready: true,
      plan_source: "test",
    };
  });

  return {
    source: "test",
    title: carousels[0]?.title || "Demo carousel",
    slide_count: carousels[0]?.slide_count ?? 0,
    hooks: useHooks.map((h) => h.text),
    topics: topics.length ? topics : [MOCK_TOPICS[0].text],
    slides: carousels[0]?.slides ?? [],
    carousels,
    carousel_count: carousels.length,
    images_ready: true,
    cache_hit: true,
    generated: true,
    intent: "Reassure families that admissions is transparent and community-led.",
    layouts: {
      single_1: { layout_mode: "single_1", carousels },
      split_2: { layout_mode: "split_2", carousels },
    },
    drive_file_id: driveFileId,
  };
}

function completeTitle(raw: string) {
  const words = raw.split(/\s+/).slice(0, 8);
  return words.join(" ").replace(/[,:;–—-]+$/, "") + (raw.endsWith(".") ? "" : "");
}

export const MOCK_TRANSCRIPT_FRAMES = Array.from({ length: 8 }, (_, i) => ({
  start_sec: 40 + i * 12,
  end_sec: 50 + i * 12,
  text: MOCK_HOOKS[i % MOCK_HOOKS.length].text.slice(0, 60),
  frame_ts: 42 + i * 12,
  preview_url: FRAME(100 + i, 480, 854),
  cached: true,
}));
