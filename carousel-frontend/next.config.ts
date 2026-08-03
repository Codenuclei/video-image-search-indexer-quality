import type { NextConfig } from "next";

/**
 * Browser talks same-origin (`NEXT_PUBLIC_API_URL=/backend`).
 * Long requests are proxied by `app/backend/[...path]/route.ts` (not rewrites) —
 * Next rewrites time out at ~30s and drop extract/generate responses.
 *
 * Upstream target:
 *   Local default: http://127.0.0.1:8000
 *   Production: set API_PROXY_TARGET (Dockerfile / Railway buildArg)
 */
const nextConfig: NextConfig = {
  output: "standalone",
  // Allow HMR when the page is opened via 127.0.0.1 instead of localhost
  allowedDevOrigins: ["127.0.0.1"],
};

export default nextConfig;
