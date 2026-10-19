import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import {
  APP_SESSION_COOKIE,
  APP_SESSION_MAX_AGE_SEC,
  backendApiBaseForServer,
  sealAppSession,
  unsealAppSession,
} from "@/lib/session-cookie";

export const dynamic = "force-dynamic";

function cookieOptions(maxAge: number) {
  return {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax" as const,
    path: "/",
    maxAge,
  };
}

export async function GET() {
  const jar = cookies();
  const session = await unsealAppSession(jar.get(APP_SESSION_COOKIE)?.value);
  if (!session) {
    return NextResponse.json({ email: null, isAdmin: false }, { status: 401 });
  }

  // Refresh is_admin from Postgres so allowlist edits apply without re-login.
  // Password-admin sessions keep isAdmin when the sealed claim matches ADMIN_LOGIN_EMAIL.
  const configuredAdminEmail = (
    process.env.ADMIN_LOGIN_EMAIL || "admintech@mastersunion.org"
  )
    .trim()
    .toLowerCase();
  let isAdmin = session.isAdmin;
  try {
    const api = backendApiBaseForServer();
    const upstream = await fetch(
      `${api}/auth/is-admin?email=${encodeURIComponent(session.email)}`,
      { cache: "no-store", headers: { Accept: "application/json" } }
    );
    if (upstream.ok) {
      const data = (await upstream.json()) as { is_admin?: boolean };
      isAdmin = Boolean(data.is_admin);
    }
  } catch {
    /* keep sealed claim if backend briefly unreachable */
  }
  if (
    !isAdmin &&
    session.isAdmin &&
    session.email.trim().toLowerCase() === configuredAdminEmail
  ) {
    isAdmin = true;
  }

  const res = NextResponse.json({ email: session.email, isAdmin });
  // Sliding expiry: always re-seal so active users never hit a stale exp.
  try {
    const token = await sealAppSession({ email: session.email, isAdmin });
    res.cookies.set(APP_SESSION_COOKIE, token, cookieOptions(APP_SESSION_MAX_AGE_SEC));
  } catch {
    /* leave existing cookie if reseal fails (e.g. missing secret in prod) */
  }
  return res;
}

export async function POST(request: Request) {
  let body: { credential?: string };
  try {
    body = (await request.json()) as { credential?: string };
  } catch {
    return NextResponse.json({ detail: "Invalid JSON" }, { status: 400 });
  }
  const credential = (body.credential || "").trim();
  if (!credential) {
    return NextResponse.json({ detail: "credential required" }, { status: 400 });
  }

  const api = backendApiBaseForServer();
  let upstream: Response;
  try {
    upstream = await fetch(`${api}/auth/google-id-token`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ credential }),
      cache: "no-store",
    });
  } catch (err) {
    return NextResponse.json(
      { detail: `Backend unreachable (${api}): ${String(err)}` },
      { status: 503 }
    );
  }

  const text = await upstream.text();
  let data: { email?: string; is_admin?: boolean; detail?: string } = {};
  try {
    data = JSON.parse(text) as typeof data;
  } catch {
    /* keep empty */
  }
  if (!upstream.ok) {
    return NextResponse.json(
      { detail: data.detail || text || "Sign-in rejected" },
      { status: upstream.status }
    );
  }
  const email = (data.email || "").trim().toLowerCase();
  if (!email) {
    return NextResponse.json({ detail: "Missing email from backend" }, { status: 502 });
  }

  let token: string;
  try {
    token = await sealAppSession({
      email,
      isAdmin: Boolean(data.is_admin),
    });
  } catch (err) {
    return NextResponse.json(
      { detail: err instanceof Error ? err.message : "APP_SESSION_SECRET is not configured" },
      { status: 503 }
    );
  }
  const res = NextResponse.json({ email, isAdmin: Boolean(data.is_admin) });
  res.cookies.set(APP_SESSION_COOKIE, token, cookieOptions(APP_SESSION_MAX_AGE_SEC));
  return res;
}

export async function DELETE() {
  const res = NextResponse.json({ ok: true });
  res.cookies.set(APP_SESSION_COOKIE, "", cookieOptions(0));
  return res;
}
