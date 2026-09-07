import { describe, expect, it } from "vitest";
import { studioVideoStatus } from "./studio-video-status";

describe("studioVideoStatus", () => {
  it("shows upload waiting before the indexer claims the file", () => {
    expect(studioVideoStatus({ status: "pending" }).label).toBe(
      "Uploaded — waiting to index"
    );
    expect(studioVideoStatus({ status: "pending" }).inflight).toBe(true);
  });

  it("shows indexing while the file is processing", () => {
    expect(studioVideoStatus({ status: "processing" }).label).toBe("Indexing…");
  });

  it("shows captions after the file is processed but cues are missing", () => {
    expect(
      studioVideoStatus({ status: "processed", has_captions: false, cue_count: 0 }).label
    ).toBe("Getting captions…");
  });

  it("shows ready once cues exist", () => {
    expect(
      studioVideoStatus({ status: "processed", has_captions: true, cue_count: 12 }).label
    ).toBe("Ready · 12 cues");
    expect(
      studioVideoStatus({ status: "processed", has_captions: true, cue_count: 12 }).inflight
    ).toBe(false);
  });
});
