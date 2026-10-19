import { NextResponse } from "next/server";
import {
  APP_SESSION_COOKIE,
  APP_SESSION_MAX_AGE_SEC,
  sealAppSession,
} from "@/lib/session-cookie";

export const dynamic = "force-dynamic";

const MAX_ATTEMPTS = 5;
const WINDOW_MS = 15 * 60 * 1000;
const attempts = new Map<string, { count: number; resetAt: number }>();

function cookieOptions() {
  return {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax" as const,
    path: "/",
    maxAge: APP_SESSION_MAX_AGE_SEC,
  };
}

function clientKey(request: Request): string {
  return (
    request.headers.get("x-forwarded-for")?.split(",", 1)[0]?.trim() ||
    request.headers.get("x-real-ip")?.trim() ||
    "unknown"
  );
}

function rateLimited(key: string): boolean {
  const now = Date.now();
  const current = attempts.get(key);
  if (!current || current.resetAt <= now) {
    attempts.set(key, { count: 1, resetAt: now + WINDOW_MS });
    return false;
  }
  current.count += 1;
  return current.count > MAX_ATTEMPTS;
}

function clearAttempts(key: string) {
  attempts.delete(key);
}

async function sha256Hex(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function constantTimeEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let diff = 0;
  for (let index = 0; index < left.length; index += 1) {
    diff |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return diff === 0;
}

export async function POST(request: Request) {
  const configuredEmail = (
    process.env.ADMIN_LOGIN_EMAIL || "admintech@mastersunion.org"
  )
    .trim()
    .toLowerCase();
  const configuredHash = (process.env.ADMIN_LOGIN_PASSWORD_HASH || "").trim().toLowerCase();
  if (!configuredEmail || !configuredHash) {
    return NextResponse.json({ detail: "Admin login is not configured" }, { status: 404 });
  }

  const key = clientKey(request);
  if (rateLimited(key)) {
    return NextResponse.json(
      { detail: "Too many sign-in attempts. Try again later." },
      { status: 429 }
    );
  }

  let body: { email?: string; password?: string };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return NextResponse.json({ detail: "Invalid credentials" }, { status: 401 });
  }

  const email = (body.email || "").trim().toLowerCase();
  const passwordHash = await sha256Hex(body.password || "");
  if (
    !constantTimeEqual(email, configuredEmail) ||
    !constantTimeEqual(passwordHash, configuredHash)
  ) {
    return NextResponse.json({ detail: "Invalid credentials" }, { status: 401 });
  }

  clearAttempts(key);
  let token: string;
  try {
    token = await sealAppSession({ email: configuredEmail, isAdmin: true });
  } catch (err) {
    return NextResponse.json(
      {
        detail:
          err instanceof Error ? err.message : "APP_SESSION_SECRET is not configured",
      },
      { status: 503 }
    );
  }
  const response = NextResponse.json({ email: configuredEmail, isAdmin: true });
  response.cookies.set(APP_SESSION_COOKIE, token, cookieOptions());
  return response;
}
