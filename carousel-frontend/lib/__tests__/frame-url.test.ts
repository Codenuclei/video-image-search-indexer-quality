import { describe, expect, it } from "vitest";
import { serverErrorMessage, withoutCacheOnly } from "../api";

describe("withoutCacheOnly", () => {
  it("strips cache_only wherever it appears", () => {
    expect(withoutCacheOnly("/m/frame?ts=1&cache_only=1&ar=4x5")).toBe("/m/frame?ts=1&ar=4x5");
    expect(withoutCacheOnly("/m/frame?cache_only=1&ts=1")).toBe("/m/frame?ts=1");
    expect(withoutCacheOnly("/m/frame?ts=1&cache_only=1")).toBe("/m/frame?ts=1");
  });
  it("returns null when nothing to strip", () => {
    expect(withoutCacheOnly("/m/frame?ts=1")).toBeNull();
  });
});

describe("serverErrorMessage", () => {
  it("names the action for bare 500s and gateway errors", () => {
    expect(serverErrorMessage("/search/carousel/pipeline/extract", 500, "Internal Server Error")).toMatch(
      /topic extract/
    );
    expect(serverErrorMessage("/search/carousel/pipeline/generate", 504, "")).toMatch(/copy generation/);
  });
  it("defers to caller for structured errors", () => {
    expect(serverErrorMessage("/x", 500, '{"detail":"boom"}')).toBeNull();
  });
});
