import { describe, expect, it } from "vitest";
import {
  guidedFolderSaveNote,
  guidedIndexSummaryNote,
  guidedPrimaryAction,
  guidedProgressLabel,
} from "../guided-drive";

describe("guidedPrimaryAction", () => {
  it("asks to connect when disconnected", () => {
    expect(guidedPrimaryAction({ connected: false })).toEqual({
      kind: "connect",
      label: "Connect Google Drive",
    });
  });

  it("asks for a folder when connected without one", () => {
    expect(guidedPrimaryAction({ connected: true, email: "a@b.com" })).toEqual({
      kind: "choose_folder",
      label: "Choose a folder",
    });
  });

  it("asks to choose videos when a folder is active", () => {
    expect(
      guidedPrimaryAction({
        connected: true,
        selected_folder: { id: "f1", name: "Events" },
      })
    ).toEqual({
      kind: "choose_videos",
      label: "Choose videos to index",
    });
  });
});

describe("guidedFolderSaveNote", () => {
  it("distinguishes reuse vs sync start", () => {
    expect(guidedFolderSaveNote("Events", true)).toBe('Existing index ready for “Events”.');
    expect(guidedFolderSaveNote("Events", false)).toBe('Folder sync started for “Events”.');
  });
});

describe("guidedIndexSummaryNote", () => {
  it("never claims indexing finished for queued work", () => {
    const note = guidedIndexSummaryNote({
      requestCount: 2,
      items: [
        {
          drive_file_id: "a",
          status: "pending",
          queued: true,
          ok: true,
        },
        {
          drive_file_id: "b",
          status: "processed",
          has_captions: true,
          cue_count: 12,
          ok: true,
        },
      ],
    });
    expect(note).toContain("queued for indexing");
    expect(note).toContain("already ready");
    expect(note.toLowerCase()).not.toContain("indexed successfully");
  });
});

describe("guidedProgressLabel", () => {
  it("maps statuses to queued / transcribing / ready / failed", () => {
    expect(guidedProgressLabel({ status: "pending", queued: true }).label).toBe("Queued");
    expect(guidedProgressLabel({ status: "processing" }).label).toBe(
      "Downloading/transcribing…"
    );
    expect(
      guidedProgressLabel({ status: "processed", has_captions: true, cue_count: 3 }).label
    ).toBe("Ready · 3 cues");
    expect(guidedProgressLabel({ status: "error" }).label).toBe("Failed");
  });
});
