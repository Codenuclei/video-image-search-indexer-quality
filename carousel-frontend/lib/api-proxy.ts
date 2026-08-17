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
  } catch {
    return NextResponse.json(
      { detail: "Can't reach the API right now. It may be starting up or temporarily unavailable." },
      { status: 502 }
    );
  }

  const outHeaders = new Headers();
  upstream.headers.forEach((value, key) => {
    if (!HOP_BY_HOP.has(key.toLowerCase())) outHeaders.set(key, value);
  });

  const contentType = upstream.headers.get("content-type")?.toLowerCase() ?? "";
  const streamBody =
    method === "GET" &&
    upstream.body !== null &&
    (upstream.status === 206 ||
      contentType.startsWith("video/") ||
      contentType === "application/octet-stream");

  // Video and byte-range responses must retain backpressure end-to-end. Since
  // Accept-Encoding is identity, the upstream stream can be passed through
  // without buffering or stale compression metadata.
  if (streamBody) {
    const contentLength = upstream.headers.get("content-length");
    if (contentLength) outHeaders.set("content-length", contentLength);
    return new NextResponse(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: outHeaders,
    });
  }

  // Preserve the existing buffered JSON/error behavior.
  const body = await upstream.arrayBuffer();
  return new NextResponse(body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: outHeaders,
  });
}
