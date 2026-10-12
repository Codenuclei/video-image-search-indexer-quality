import { describe, expect, it } from "vitest";
import {
  carouselsFromSavePayload,
  parseSaveIdParam,
  restoreCarouselSaveState,
  saveMatchesVideo,
} from "./carousel-save-restore";

describe("carousel-save-restore", () => {
  it("parses save ids and rejects junk", () => {
    expect(parseSaveIdParam("42")).toBe(42);
    expect(parseSaveIdParam("0")).toBeNull();
    expect(parseSaveIdParam("-1")).toBeNull();
    expect(parseSaveIdParam("1.5")).toBeNull();
    expect(parseSaveIdParam("abc")).toBeNull();
    expect(parseSaveIdParam(null)).toBeNull();
  });

  it("validates drive_file_id association", () => {
    expect(saveMatchesVideo("vid-1", "vid-1")).toBe(true);
    expect(saveMatchesVideo("vid-1", "vid-2")).toBe(false);
    expect(saveMatchesVideo(null, "vid-1")).toBe(false);
  });

  it("restores from carousels payload", () => {
    const state = restoreCarouselSaveState(
      {
        carousels: [
          {
            id: "c1",
            title: "Hook one",
            slides: [{ preview_url: "/media/video/x/frame?ts=1" }],
            images_ready: true,
          },
        ],
      },
      "Label"
    );
    expect(state).not.toBeNull();
    expect(state!.carousels).toHaveLength(1);
    expect(state!.activeCarouselId).toBe("c1");
    expect(state!.imagesReady).toBe(true);
    expect(state!.title).toBe("Hook one");
  });

  it("restores legacy slides-only payload", () => {
    const list = carouselsFromSavePayload(
      { slides: [{ preview_url: "/media/a" }, { preview_url: "/media/b" }] },
      "Legacy"
    );
    expect(list).toHaveLength(1);
    expect(list[0].slide_count).toBe(2);
    expect(list[0].title).toBe("Legacy");
  });

  it("restores the saved layout rather than always preferring single-image", () => {
    const state = restoreCarouselSaveState(
      {
        layouts: {
          single_1: {
            carousels: [{ id: "single", slides: [{ preview_url: "/media/single" }] }],
          },
          split_2: {
            carousels: [{ id: "split", slides: [{ preview_url: "/media/split" }] }],
          },
        },
      },
      "Saved split layout",
      "split_2"
    );
    expect(state?.activeCarouselId).toBe("split");
    expect(state?.carousels[0].id).toBe("split");
  });

  it("recognizes panel previews and ignores malformed carousel entries", () => {
    const state = restoreCarouselSaveState({
      carousels: [
        "broken" as unknown as { id: string },
        {
          id: "valid",
          slides: [{ panels: [{ preview_url: "/media/panel" }] }],
        },
      ],
    });
    expect(state?.carousels).toHaveLength(1);
    expect(state?.activeCarouselId).toBe("valid");
    expect(state?.imagesReady).toBe(true);
  });

  it("returns null when empty / malformed", () => {
    expect(restoreCarouselSaveState(null)).toBeNull();
    expect(restoreCarouselSaveState({})).toBeNull();
    expect(restoreCarouselSaveState({ carousels: [] })).toBeNull();
  });
});
