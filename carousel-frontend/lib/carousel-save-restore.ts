/**
 * Pure helpers for restoring a carousel generation save into studio state.
 * Restoring only maps payload → UI state; it never mutates the saved row.
 */

export type RestorableCarouselSlide = {
  preview_url?: string | null;
  panels?: Array<{ preview_url?: string | null } | null> | null;
  [key: string]: unknown;
};

export type RestorableCarouselItem = {
  id?: string;
  kind?: string;
  title?: string;
  topic_labels?: string[];
  slide_count?: number;
  slides?: RestorableCarouselSlide[];
  images_ready?: boolean;
  [key: string]: unknown;
};

export type CarouselSavePayloadLike = {
  carousels?: RestorableCarouselItem[] | null;
  slides?: RestorableCarouselSlide[] | null;
  layouts?: {
    single_1?: { carousels?: RestorableCarouselItem[] | null };
    split_2?: { carousels?: RestorableCarouselItem[] | null };
  } | null;
  title?: string | null;
  images_ready?: boolean;
  [key: string]: unknown;
};

export type RestoredCarouselState = {
  carousels: RestorableCarouselItem[];
  layouts: {
    single_1?: { carousels: RestorableCarouselItem[] };
    split_2?: { carousels: RestorableCarouselItem[] };
  } | null;
  activeCarouselId: string | null;
  imagesReady: boolean;
  title: string;
};

/** Normalize carousels list from a save payload (carousels array or legacy slides). */
export function carouselsFromSavePayload(
  payload: CarouselSavePayloadLike | null | undefined,
  label?: string | null
): RestorableCarouselItem[] {
  if (!payload || typeof payload !== "object") return [];
  const fromList = Array.isArray(payload.carousels)
    ? payload.carousels.filter(
        (item): item is RestorableCarouselItem =>
          Boolean(item) && typeof item === "object" && !Array.isArray(item)
      )
    : [];
  if (fromList.length) {
    return fromList.map((c, idx) => ({
      ...c,
      id: String(c.id || `restored_${idx + 1}`),
      slides: Array.isArray(c.slides) ? c.slides : [],
      slide_count: c.slide_count ?? (Array.isArray(c.slides) ? c.slides.length : 0),
      images_ready: Boolean(c.images_ready ?? payload.images_ready),
    }));
  }
  const slides = Array.isArray(payload.slides) ? payload.slides : [];
  if (!slides.length) return [];
  return [
    {
      id: "restored",
      kind: "mixed",
      title: label || payload.title || "Carousel",
      topic_labels: [],
      slide_count: slides.length,
      slides,
      images_ready: Boolean(payload.images_ready ?? true),
    },
  ];
}

export function layoutsFromSavePayload(
  payload: CarouselSavePayloadLike | null | undefined
): RestoredCarouselState["layouts"] {
  if (!payload?.layouts || typeof payload.layouts !== "object") return null;
  const single = payload.layouts.single_1?.carousels;
  const split = payload.layouts.split_2?.carousels;
  const out: RestoredCarouselState["layouts"] = {};
  if (Array.isArray(single) && single.length) {
    out.single_1 = {
      carousels: single
        .filter(
          (item): item is RestorableCarouselItem =>
            Boolean(item) && typeof item === "object" && !Array.isArray(item)
        )
        .map((c, i) => ({
        ...c,
        id: String(c.id || `hook_${i + 1}`),
        slides: Array.isArray(c.slides) ? c.slides : [],
      })),
    };
  }
  if (Array.isArray(split) && split.length) {
    out.split_2 = {
      carousels: split
        .filter(
          (item): item is RestorableCarouselItem =>
            Boolean(item) && typeof item === "object" && !Array.isArray(item)
        )
        .map((c, i) => ({
        ...c,
        id: String(c.id || `hook_${i + 1}`),
        slides: Array.isArray(c.slides) ? c.slides : [],
      })),
    };
  }
  return out.single_1 || out.split_2 ? out : null;
}

/** Build studio-ready state from an exact save payload. */
export function restoreCarouselSaveState(
  payload: CarouselSavePayloadLike | null | undefined,
  label?: string | null,
  layoutMode?: string | null
): RestoredCarouselState | null {
  const layouts = layoutsFromSavePayload(payload);
  const carousels = carouselsFromSavePayload(payload, label);
  const preferredLayout =
    layoutMode === "split_2"
      ? layouts?.split_2?.carousels
      : layoutMode === "single_1"
        ? layouts?.single_1?.carousels
        : null;
  const fromLayout =
    preferredLayout?.length
      ? preferredLayout
      : layouts?.single_1?.carousels?.length
      ? layouts.single_1.carousels
      : layouts?.split_2?.carousels?.length
        ? layouts.split_2.carousels
        : carousels;
  if (!fromLayout.length) return null;
  const first = fromLayout[0];
  const imagesReady = Boolean(
    first.images_ready ||
      first.slides?.some(
        (slide) =>
          slide &&
          typeof slide === "object" &&
          (slide.preview_url ||
            slide.panels?.some(
              (panel) => panel && typeof panel === "object" && panel.preview_url
            ))
      ) ||
      payload?.images_ready
  );
  return {
    carousels: fromLayout,
    layouts,
    activeCarouselId: first.id ? String(first.id) : null,
    imagesReady,
    title: String(first.title || label || payload?.title || "Carousel"),
  };
}

/** Validate that a save belongs to the requested video (frontend guard). */
export function saveMatchesVideo(
  saveDriveFileId: string | null | undefined,
  videoId: string | null | undefined
): boolean {
  if (!saveDriveFileId || !videoId) return false;
  return saveDriveFileId.trim() === videoId.trim();
}

/** Parse positive integer save id from a query string value. */
export function parseSaveIdParam(raw: string | null | undefined): number | null {
  if (raw == null || raw === "") return null;
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0 || !Number.isInteger(n)) return null;
  return n;
}
