/**
 * Durable same-origin proxy to the FastAPI backend.
 *
 * Important: Node `fetch` auto-decompresses upstream bodies. If we forward the
 * client's Accept-Encoding and then re-emit content-encoding, browsers/curl get
 * empty or corrupt JSON (HTTP 200, 0 bytes) — which surfaces as
 * "Can't reach the studio API" in Carousel Studio.
 */
import { NextRequest, NextResponse } from "next/server";

export const API_PROXY_TARGET = (
  process.env.API_PROXY_TARGET ?? "http://127.0.0.1:8000"
).replace(/\/+$/, "");

const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailers",
  "transfer-encoding",
  "upgrade",
  "host",
  "content-length",
  // fetch() decompresses; never re-advertise compression to the client
  "content-encoding",
  "accept-encoding",
]);

export async function proxyToBackend(
  req: NextRequest,
  pathParts: string[]
): Promise<Response> {
  const upstreamPath = pathParts.map(encodeURIComponent).join("/");
  const url = `${API_PROXY_TARGET}/${upstreamPath}${req.nextUrl.search}`;

  const headers = new Headers();
  req.headers.forEach((value, key) => {
    if (!HOP_BY_HOP.has(key.toLowerCase())) headers.set(key, value);
  });
  // Ask upstream for plain bytes so Node fetch + our buffer stay consistent.
  headers.set("accept-encoding", "identity");

  const method = req.method.toUpperCase();
  const init: RequestInit = { method, headers, cache: "no-store" };
  if (method !== "GET" && method !== "HEAD") {
    init.body = await req.arrayBuffer();
  }

  let upstream: Response;
  try {
    upstream = await fetch(url, init);
  } catch (err) {
    const message = err instanceof Error ? err.message : "Upstream fetch failed";
    return NextResponse.json(
      { detail: `Backend unreachable (${API_PROXY_TARGET}): ${message}` },
      { status: 502 }
    );
  }

  const outHeaders = new Headers();
  upstream.headers.forEach((value, key) => {
    if (!HOP_BY_HOP.has(key.toLowerCase())) outHeaders.set(key, value);
  });

  // Buffer so we never stream a decompressed body under a stale content-encoding.
  const body = await upstream.arrayBuffer();
  return new NextResponse(body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: outHeaders,
  });
}
