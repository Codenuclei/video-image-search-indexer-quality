import { describe, expect, it } from "vitest";
import { resolveRedirectLocation } from "./api-proxy-redirect";

describe("resolveRedirectLocation", () => {
  it("keeps Google's absolute OAuth URL", () => {
    expect(
      resolveRedirectLocation(
        "https://accounts.google.com/o/oauth2/v2/auth?client_id=x",
        "https://dfi-carousel-backend-production.up.railway.app/auth/google"
      )
    ).toBe("https://accounts.google.com/o/oauth2/v2/auth?client_id=x");
  });

  it("resolves a relative Location against the upstream API", () => {
    expect(
      resolveRedirectLocation(
        "/carousel?connected=1",
        "https://dfi-carousel-backend-production.up.railway.app/auth/google/callback"
      )
    ).toBe("https://dfi-carousel-backend-production.up.railway.app/carousel?connected=1");
  });

  it("returns null for missing Location", () => {
    expect(resolveRedirectLocation(null, "https://example.com/auth/google")).toBeNull();
    expect(resolveRedirectLocation("   ", "https://example.com/auth/google")).toBeNull();
  });
});
