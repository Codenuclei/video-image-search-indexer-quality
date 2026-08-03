/**
 * Same-origin API proxy for carousel studio.
 *
 * Next.js config `rewrites()` to an upstream die at ~30s with a bare 500, which
 * kills long carousel extract/generate calls while the backend keeps working.
 * This Route Handler waits for the upstream response with no short proxy timeout.
 */
import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
/** App-router hint (Vercel / platforms that honor it). Local `next start` waits fully. */
export const maxDuration = 300;

const API_PROXY_TARGET = (
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
]);

async function proxy(req: NextRequest, pathParts: string[]): Promise<Response> {
  const upstreamPath = pathParts.map(encodeURIComponent).join("/");
  const url = `${API_PROXY_TARGET}/${upstreamPath}${req.nextUrl.search}`;

  const headers = new Headers();
  req.headers.forEach((value, key) => {
    if (!HOP_BY_HOP.has(key.toLowerCase())) headers.set(key, value);
  });

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

  return new NextResponse(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: outHeaders,
  });
}

type Ctx = { params: Promise<{ path: string[] }> };

async function handle(req: NextRequest, ctx: Ctx) {
  const { path } = await ctx.params;
  return proxy(req, path ?? []);
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
export const OPTIONS = handle;
export const HEAD = handle;
