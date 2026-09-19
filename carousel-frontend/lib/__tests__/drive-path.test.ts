import { describe, expect, it } from "vitest";
import { driveFileOpenUrl, driveFolderPath } from "../drive-path";

describe("driveFolderPath", () => {
  it("strips the file name from a nested path", () => {
    expect(
      driveFolderPath(
        "Fireside Chat/Cam 1 Master/C0001.MP4",
        "C0001.MP4"
      )
    ).toBe("Fireside Chat/Cam 1 Master");
  });

  it("returns empty when path is only the file name", () => {
    expect(driveFolderPath("C0001.MP4", "C0001.MP4")).toBe("");
  });
});

describe("driveFileOpenUrl", () => {
  it("builds a Drive file view URL", () => {
    expect(driveFileOpenUrl("1abcXYZ")).toBe(
      "https://drive.google.com/file/d/1abcXYZ/view"
    );
  });
});
