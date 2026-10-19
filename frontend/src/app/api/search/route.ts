/**
 * Same-origin search proxy: forwards GET /api/search to backend /search and
 * attaches Authorization: Bearer from the httpOnly session cookie when present
 * so production search can attribute the caller in audit logs.
 *
 * Upstream uses AbortSignal.timeout(60s). AbortSignal.any is not assumed
 * (Node/Next 14); client disconnect still ends when the timeout fires.
 */
import { cookies } from "next/headers";
import { NextRequest, NextResponse } from "next/server";
import {
  APP_SESSION_COOKIE,
  backendApiBaseForServer,
  unsealAppSession,
} from "@/lib/session-cookie";

export const dynamic = "force-dynamic";

const UPSTREAM_TIMEOUT_MS = 60_000;

function isAbortError(err: unknown): boolean {
  if (!err || typeof err !== "object") return false;
  const name = (err as { name?: string }).name;
  return name === "AbortError" || name === "TimeoutError";
}

export async function GET(request: NextRequest) {
  const search = new URL(request.url).search;
  const upstreamUrl = `${backendApiBaseForServer()}/search${search}`;

  const headers: Record<string, string> = {
    Accept: "application/json",
  };

  const forwardedFor = request.headers.get("x-forwarded-for");
  const realIp = request.headers.get("x-real-ip");
  if (forwardedFor) headers["X-Forwarded-For"] = forwardedFor;
  else if (realIp) headers["X-Forwarded-For"] = realIp;

  const jar = cookies();
  const cookieVal = jar.get(APP_SESSION_COOKIE)?.value;
  if (cookieVal) {
    const session = await unsealAppSession(cookieVal);
    if (session) {
      headers.Authorization = `Bearer ${cookieVal}`;
    }
  }

  let upstream: Response;
  try {
    upstream = await fetch(upstreamUrl, {
      method: "GET",
      headers,
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
  } catch (err) {
    if (isAbortError(err)) {
      return NextResponse.json({ detail: "Upstream timeout" }, { status: 504 });
    }
    return NextResponse.json(
      { detail: `Backend unreachable: ${String(err)}` },
      { status: 502 }
    );
  }

  const text = await upstream.text();
  const contentType = upstream.headers.get("Content-Type") || "application/json";
  return new NextResponse(text, {
    status: upstream.status,
    headers: { "Content-Type": contentType },
  });
}
