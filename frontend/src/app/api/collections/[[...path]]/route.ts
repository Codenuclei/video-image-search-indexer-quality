/**
 * Same-origin BFF for /collections: reads the httpOnly session cookie and
 * forwards Authorization: Bearer to the backend. The sealed token never reaches page JS.
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

function forbidOrigin(request: NextRequest): NextResponse | null {
  const origin = request.headers.get("origin");
  if (!origin) return null;
  try {
    const originHost = new URL(origin).host;
    const host = request.headers.get("host") || "";
    if (originHost !== host) {
      return NextResponse.json({ detail: "Forbidden origin" }, { status: 403 });
    }
  } catch {
    return NextResponse.json({ detail: "Forbidden origin" }, { status: 403 });
  }
  return null;
}

async function proxyCollections(request: NextRequest, context: RouteContext) {
  const method = request.method.toUpperCase();

  if (method === "POST" || method === "PATCH" || method === "DELETE") {
    const blocked = forbidOrigin(request);
    if (blocked) return blocked;
  }

  if (method === "POST" || method === "PATCH") {
    const ct = (request.headers.get("content-type") || "").toLowerCase();
    if (!ct.includes("application/json")) {
      return NextResponse.json(
        { detail: "Content-Type must be application/json" },
        { status: 415 }
      );
    }
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

  const parts = context.params.path ?? [];
  const subpath = parts.map((p) => encodeURIComponent(p)).join("/");
  const search = new URL(request.url).search;
  const upstreamUrl = `${backendApiBaseForServer()}/collections${
    subpath ? `/${subpath}` : ""
  }${search}`;

  const headers: Record<string, string> = {
    Authorization: `Bearer ${cookieVal}`,
    Accept: "application/json",
  };

  let body: string | undefined;
  if (method === "POST" || method === "PATCH") {
    body = await request.text();
    headers["Content-Type"] = "application/json";
  }

  let upstream: Response;
  try {
    upstream = await fetch(upstreamUrl, {
      method,
      headers,
      body,
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

  if (upstream.status === 204) {
    return new NextResponse(null, { status: 204 });
  }

  const text = await upstream.text();
  const contentType = upstream.headers.get("Content-Type") || "application/json";
  return new NextResponse(text, {
    status: upstream.status,
    headers: { "Content-Type": contentType },
  });
}

export async function GET(request: NextRequest, context: RouteContext) {
  return proxyCollections(request, context);
}

export async function POST(request: NextRequest, context: RouteContext) {
  return proxyCollections(request, context);
}

export async function PATCH(request: NextRequest, context: RouteContext) {
  return proxyCollections(request, context);
}

export async function DELETE(request: NextRequest, context: RouteContext) {
  return proxyCollections(request, context);
}
