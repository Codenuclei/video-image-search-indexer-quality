import { describe, expect, it } from "vitest";
import { duplicateVideoNames, studioVideoStatus, videoDisambiguator } from "./studio-video-status";

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

describe("duplicate video names", () => {
  it("flags names that appear more than once (case-insensitive)", () => {
    const dupes = duplicateVideoNames([
      { name: "Talk.mp4" },
      { name: "talk.MP4" },
      { name: "Other.mp4" },
    ]);
    expect(dupes.has("talk.mp4")).toBe(true);
    expect(dupes.has("other.mp4")).toBe(false);
  });

  it("disambiguates with folder path and id suffix", () => {
    expect(
      videoDisambiguator({ id: "abcdef123456", name: "Talk.mp4", path: "/Root/Sub/Talk.mp4" })
    ).toBe("/Root/Sub · id …123456");
    expect(videoDisambiguator({ id: "abcdef123456", name: "Talk.mp4", path: null })).toBe(
      "id …123456"
    );
  });
});
