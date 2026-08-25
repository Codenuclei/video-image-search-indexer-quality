import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CarouselOutlineSlide } from "./api";

vi.mock("./api", () => ({
  apiAssetUrl: (url: string) => url,
  cacheOnlyAssetUrl: (url: string) => url,
}));

import {
  CAROUSEL_EXPORT_HEIGHT,
  CAROUSEL_EXPORT_WIDTH,
  CarouselExportError,
  carouselArchiveFilename,
  carouselSlideFilename,
  carouselSlideFilenames,
  computeCoverCrop,
  renderCarouselSlide,
  renderCarouselSlideToCanvas,
  resolveHighlightIndices,
  sanitizeExportFilename,
  validateSlideForExport,
} from "./carousel-export";

function slide(overrides: Partial<CarouselOutlineSlide> = {}): CarouselOutlineSlide {
  return {
    index: 0,
    hook_line: "Build the useful system",
    transcript_text: "Build the useful system",
    drive_file_id: "video-1",
    name: "Video",
    timestamp_sec: 1,
    moment_index: 0,
    preview_url: "https://images.test/frame-1.jpg",
    frame_source: "manual",
    ...overrides,
  };
}

function canvasHarness() {
  const gradient = { addColorStop: vi.fn() };
  const context = {
    beginPath: vi.fn(),
    closePath: vi.fn(),
    createLinearGradient: vi.fn(() => gradient),
    drawImage: vi.fn(),
    fill: vi.fn(),
    fillRect: vi.fn(),
    fillText: vi.fn(),
    lineTo: vi.fn(),
    measureText: vi.fn((text: string) => ({ width: text.length * 20 })),
    moveTo: vi.fn(),
    quadraticCurveTo: vi.fn(),
    stroke: vi.fn(),
    fillStyle: "",
    font: "",
    lineWidth: 0,
    shadowBlur: 0,
    shadowColor: "",
    shadowOffsetY: 0,
    strokeStyle: "",
    textAlign: "left",
    textBaseline: "alphabetic",
  } as unknown as CanvasRenderingContext2D;
  const canvas = {
    width: 0,
    height: 0,
    getContext: vi.fn(() => context),
    toBlob: vi.fn((callback: BlobCallback) =>
      callback(new Blob(["jpeg"], { type: "image/jpeg" }))
    ),
  } as unknown as HTMLCanvasElement;
  return { canvas, context };
}

class LoadedImage {
  naturalWidth = 2000;
  naturalHeight = 1000;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;

  set src(_value: string) {
    this.onload?.();
  }
}

describe("carousel export helpers", () => {
  it("sanitizes filenames and creates stable ordered slide names", () => {
    expect(sanitizeExportFilename("  Déjà Vu: Growth / 2026  ")).toBe("deja-vu-growth-2026");
    expect(sanitizeExportFilename("***")).toBe("instagram-carousel");
    expect(carouselSlideFilename("My Post", 0)).toBe("my-post-01.jpg");
    expect(carouselSlideFilenames("My Post", 3)).toEqual([
      "my-post-01.jpg",
      "my-post-02.jpg",
      "my-post-03.jpg",
    ]);
    expect(carouselArchiveFilename("My Post")).toBe("my-post-carousel.zip");
  });

  it("computes a focal-point-aware cover crop", () => {
    const crop = computeCoverCrop(2000, 1000, 1080, 1350, 1, 0);
    expect(crop.sx).toBeCloseTo(1200);
    expect(crop.sy).toBeCloseTo(0);
    expect(crop.sw).toBeCloseTo(800);
    expect(crop.sh).toBeCloseTo(1000);
  });

  it("resolves valid indices first and falls back to matching words", () => {
    expect([...resolveHighlightIndices("One bold word", [1, 99], ["word"])]).toEqual([1]);
    expect([
      ...resolveHighlightIndices("Go, go build!", null, ["go", "go", "build"]),
    ]).toEqual([0, 1, 2]);
  });
});

describe("carousel export validation", () => {
  it("accepts a selected single image and rejects a missing one clearly", () => {
    expect(() => validateSlideForExport(slide(), "single_1", 1)).not.toThrow();
    expect(() =>
      validateSlideForExport(slide({ preview_url: "  " }), "single_1", 2)
    ).toThrowError(/Slide 2 has no selected image/);
  });

  it("requires two selected, distinct split images", () => {
    const valid = slide({
      panels: [
        { preview_url: "/frame?ts=1&cache_only=1" },
        { preview_url: "/frame?ts=2&cache_only=1" },
      ],
    });
    expect(() => validateSlideForExport(valid, "split_2", 1)).not.toThrow();
    expect(() =>
      validateSlideForExport(
        slide({ panels: [{ preview_url: "/one.jpg" }, { preview_url: "" }] }),
        "split_2",
        3
      )
    ).toThrowError(/Slide 3 needs two distinct selected panel images/);
    expect(() =>
      validateSlideForExport(
        slide({
          panels: [
            { preview_url: "/frame?ts=1" },
            { preview_url: "/frame?cache_only=1&ts=1" },
          ],
        }),
        "split_2",
        4
      )
    ).toThrowError(/Slide 4 needs two distinct selected panel images/);
  });
});

describe("carousel slide rendering", () => {
  const createObjectURL = vi.fn(() => "blob:frame");
  const revokeObjectURL = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("Image", LoadedImage);
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(new Blob(["image"]), { status: 200 }))
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("renders and encodes an exact 1080x1350 canvas", async () => {
    const { canvas } = canvasHarness();
    vi.stubGlobal("document", { createElement: vi.fn(() => canvas) });

    const result = await renderCarouselSlide(slide(), "single_1", 0, 1);

    expect(canvas.width).toBe(CAROUSEL_EXPORT_WIDTH);
    expect(canvas.height).toBe(CAROUSEL_EXPORT_HEIGHT);
    expect(canvas.width).toBe(1080);
    expect(canvas.height).toBe(1350);
    expect(canvas.toBlob).toHaveBeenCalledWith(expect.any(Function), "image/jpeg", 0.92);
    expect(result.type).toBe("image/jpeg");
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:frame");
  });

  it("shares the same 1080x1350 canvas path for preview and JPEG export", async () => {
    const { canvas } = canvasHarness();
    vi.stubGlobal("document", { createElement: vi.fn(() => canvas) });

    const previewCanvas = await renderCarouselSlideToCanvas(slide(), "single_1", 0, 2);
    const jpeg = await renderCarouselSlide(slide(), "single_1", 0, 2);

    expect(previewCanvas).toBe(canvas);
    expect(previewCanvas.width).toBe(1080);
    expect(previewCanvas.height).toBe(1350);
    expect(jpeg.type).toBe("image/jpeg");
  });

  it("creates a preview object URL that the caller can revoke", async () => {
    const { canvas } = canvasHarness();
    vi.stubGlobal("document", { createElement: vi.fn(() => canvas) });
    const { renderCarouselSlidePreviewUrl } = await import("./carousel-export");

    const url = await renderCarouselSlidePreviewUrl(slide(), "single_1", 0, 1);
    expect(url).toMatch(/^blob:/);
    expect(createObjectURL).toHaveBeenCalled();
  });

  it("renders both selected images in split-panel layout", async () => {
    const { canvas, context } = canvasHarness();
    vi.stubGlobal("document", { createElement: vi.fn(() => canvas) });
    const splitSlide = slide({
      highlight_words: ["useful"],
      panels: [
        { preview_url: "https://images.test/frame-1.jpg", caption: "Build the useful" },
        { preview_url: "https://images.test/frame-2.jpg", caption: "system first" },
      ],
    });

    await renderCarouselSlide(splitSlide, "split_2", 0, 1);

    expect(context.drawImage).toHaveBeenCalledTimes(2);
    expect(canvas.width).toBe(1080);
    expect(canvas.height).toBe(1350);
  });

  it("reports a clear CORS/network fetch failure", async () => {
    const { canvas } = canvasHarness();
    vi.stubGlobal("document", { createElement: vi.fn(() => canvas) });
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));

    await expect(renderCarouselSlide(slide(), "single_1", 0, 1)).rejects.toEqual(
      expect.objectContaining<Partial<CarouselExportError>>({
        name: "CarouselExportError",
        message: expect.stringMatching(/could not be fetched.*allows CORS/i),
      })
    );
  });
});
