import { describe, expect, it } from "vitest";
import { qualityUiStatus, shouldHideQualityScore } from "./carousel-quality";

describe("carousel quality UI status", () => {
  it("marks a missing or stale report as needing recheck", () => {
    expect(qualityUiStatus({ report: { score: 80 }, stale: true, rechecking: false })).toBe(
      "stale"
    );
    expect(qualityUiStatus({ report: null, stale: false, rechecking: false })).toBe("stale");
    expect(shouldHideQualityScore("stale")).toBe(true);
  });

  it("shows rechecking ahead of a stale cached score", () => {
    expect(
      qualityUiStatus({ report: { score: 80 }, stale: true, rechecking: true })
    ).toBe("rechecking");
    expect(shouldHideQualityScore("rechecking")).toBe(true);
  });

  it("keeps a current report visible", () => {
    expect(
      qualityUiStatus({ report: { score: 82 }, stale: false, rechecking: false })
    ).toBe("current");
    expect(shouldHideQualityScore("current")).toBe(false);
  });
});
