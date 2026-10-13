import { describe, expect, it } from "vitest";
import {
  deserializeStudioDraft,
  isDraftStale,
  parseDraftParam,
  serializeStudioDraft,
} from "./carousel-draft";

describe("carousel-draft", () => {
  it("parses draft query param", () => {
    expect(parseDraftParam("1")).toBe(true);
    expect(parseDraftParam("true")).toBe(true);
    expect(parseDraftParam("yes")).toBe(true);
    expect(parseDraftParam("0")).toBe(false);
    expect(parseDraftParam("")).toBe(false);
    expect(parseDraftParam(null)).toBe(false);
  });

  it("serializes studio state with defaults", () => {
    const body = serializeStudioDraft({
      drive_file_id: " vid-1 ",
      phase: 3 as const,
      selected_hooks: ["a"],
      images_ready: true,
    });
    expect(body.drive_file_id).toBe("vid-1");
    expect(body.phase).toBe(3);
    expect(body.selected_hooks).toEqual(["a"]);
    expect(body.selected_themes).toEqual([]);
    expect(body.layout_mode).toBe("single_1");
    expect(body.images_ready).toBe(true);
    expect(body.client_updated_at).toBeTruthy();
  });

  it("deserializes malformed server payloads safely", () => {
    const state = deserializeStudioDraft({
      id: 1,
      drive_file_id: "vid-1",
      payload: {
        phase: "99",
        carousels: "nope" as unknown as [],
        layouts: null,
      },
    });
    expect(state).not.toBeNull();
    expect(state!.phase).toBe(1);
    expect(state!.carousels).toEqual([]);
    expect(state!.drive_file_id).toBe("vid-1");
  });

  it("returns null when drive_file_id missing", () => {
    expect(deserializeStudioDraft({ id: 1, drive_file_id: "", payload: {} })).toBeNull();
    expect(deserializeStudioDraft(null)).toBeNull();
  });

  it("detects stale drafts via timestamps", () => {
    expect(
      isDraftStale("2026-03-01T10:00:00.000Z", "2026-03-01T11:00:00.000Z")
    ).toBe(true);
    expect(
      isDraftStale("2026-03-01T12:00:00.000Z", "2026-03-01T11:00:00.000Z")
    ).toBe(false);
    // Equal baselines (own previous write) must not look stale.
    expect(
      isDraftStale("2026-03-01T11:00:00.000Z", "2026-03-01T11:00:00.000Z")
    ).toBe(false);
    expect(isDraftStale(null, "2026-03-01T11:00:00.000Z")).toBe(false);
    expect(isDraftStale("not-a-date", "2026-03-01T11:00:00.000Z")).toBe(false);
  });
});
