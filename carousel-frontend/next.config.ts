import type { NextConfig } from "next";

/**
 * Browser talks same-origin (`NEXT_PUBLIC_API_URL=/backend`);
 * Next proxies `/backend/*` → FastAPI (avoids CORS).
 *
 * Local default: http://127.0.0.1:8000
 * Production (Railway): https://dfi-backend-production.up.railway.app
 *   — set via Dockerfile / railway.json buildArg API_PROXY_TARGET
 */
const API_PROXY_TARGET = (
  process.env.API_PROXY_TARGET ?? "http://127.0.0.1:8000"
).replace(/\/+$/, "");

const nextConfig: NextConfig = {
  output: "standalone",
  // Allow HMR when the page is opened via 127.0.0.1 instead of localhost
  allowedDevOrigins: ["127.0.0.1"],
  async rewrites() {
    return [
      {
        source: "/backend/:path*",
        destination: `${API_PROXY_TARGET}/:path*`,
      },
    ];
  },
};

export default nextConfig;
