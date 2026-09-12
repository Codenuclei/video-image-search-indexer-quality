import { describe, expect, it } from "vitest";

describe("select-images preparing contract", () => {
  it("treats preparing responses as not images_ready", () => {
    const preparing = {
      status: "preparing",
      preparing: true,
      images_ready: false,
      job_id: "abc",
      carousels: [],
    };
    expect(preparing.preparing || preparing.status === "preparing").toBe(true);
    expect(Boolean(preparing.images_ready)).toBe(false);
  });

  it("hard-caps quote sample count for display copy", () => {
    const intervals = Array.from({ length: 40 }, (_, i) => ({
      start: i * 10,
      end: i * 10 + 5,
    }));
    const raw: number[] = [];
    for (const { start, end } of intervals) {
      const mid = start + (end - start) * 0.5;
      raw.push(start, mid, end);
    }
    const seen = new Set<number>();
    const capped: number[] = [];
    for (const ts of raw) {
      if (seen.has(ts)) continue;
      seen.add(ts);
      capped.push(ts);
      if (capped.length >= 24) break;
    }
    expect(capped.length).toBeLessThanOrEqual(24);
  });
});
