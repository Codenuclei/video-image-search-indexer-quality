/**
 * Same-origin BFF for /admin/search-audit: requires sealed isAdmin session,
 * then forwards Authorization: Bearer to the backend.
 */
import { cookies } from "next/headers";
import { NextRequest, NextResponse } from "next/server";
import {
  APP_SESSION_COOKIE,
  backendApiBaseForServer,
  unsealAppSession,
} from "@/lib/session-cookie";

export const dynamic = "force-dynamic";

type RouteContext = { params: { path?: string[] } };

const UPSTREAM_TIMEOUT_MS = 15_000;

function isAbortError(err: unknown): boolean {
  if (!err || typeof err !== "object") return false;
  const name = (err as { name?: string }).name;
  return name === "AbortError" || name === "TimeoutError";
}

async function proxySearchAudit(request: NextRequest, context: RouteContext) {
  const method = request.method.toUpperCase();
  if (method !== "GET") {
    return NextResponse.json({ detail: "Method not allowed" }, { status: 405 });
  }

  const jar = cookies();
  const cookieVal = jar.get(APP_SESSION_COOKIE)?.value;
  const session = await unsealAppSession(cookieVal);
  if (!session || !cookieVal) {
    return NextResponse.json(
      { detail: "Session expired — please sign out and sign in again" },
      { status: 401 }
    );
  }
  if (!session.isAdmin) {
    return NextResponse.json({ detail: "Admin access required" }, { status: 403 });
  }

  const parts = context.params.path ?? [];
  const subpath = parts.map((p) => encodeURIComponent(p)).join("/");
  const search = new URL(request.url).search;
  const upstreamUrl = `${backendApiBaseForServer()}/admin/search-audit${
    subpath ? `/${subpath}` : ""
  }${search}`;

  let upstream: Response;
  try {
    upstream = await fetch(upstreamUrl, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${cookieVal}`,
        Accept: "application/json",
      },
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

export async function GET(request: NextRequest, context: RouteContext) {
  return proxySearchAudit(request, context);
}
