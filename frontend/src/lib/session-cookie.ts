/**
 * Signed httpOnly app session cookie (Edge + Node Web Crypto).
 * Payload is verified in middleware — never trust client localStorage for /admin.
 */
export const APP_SESSION_COOKIE = "dfi_app_session";
export const APP_SESSION_MAX_AGE_SEC = 90 * 24 * 60 * 60;

export type AppSession = {
  email: string;
  isAdmin: boolean;
  exp: number;
};

function sessionSecret(): string {
  const explicit = (process.env.APP_SESSION_SECRET || "").trim();
  if (explicit) return explicit;
  // Stable fallback so local/dev works without an extra secret; production
  // should set APP_SESSION_SECRET on the frontend service.
  const cid = (process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID || "").trim();
  return `dfi-session:${cid || "dev"}`;
}

function b64urlEncode(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]!);
  const b64 = btoa(bin);
  return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function b64urlDecode(s: string): Uint8Array {
  const pad = s.length % 4 === 0 ? "" : "=".repeat(4 - (s.length % 4));
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + pad;
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function utf8(s: string): Uint8Array {
  return new TextEncoder().encode(s);
}

async function hmacSign(data: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    utf8(sessionSecret()) as BufferSource,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, utf8(data) as BufferSource);
  return b64urlEncode(new Uint8Array(sig));
}

async function hmacVerify(data: string, sigB64: string): Promise<boolean> {
  const expected = await hmacSign(data);
  if (expected.length !== sigB64.length) return false;
  let ok = 0;
  for (let i = 0; i < expected.length; i++) {
    ok |= expected.charCodeAt(i) ^ sigB64.charCodeAt(i);
  }
  return ok === 0;
}

export async function sealAppSession(session: Omit<AppSession, "exp"> & { exp?: number }): Promise<string> {
  const payload: AppSession = {
    email: session.email.trim().toLowerCase(),
    isAdmin: Boolean(session.isAdmin),
    exp: session.exp ?? Math.floor(Date.now() / 1000) + APP_SESSION_MAX_AGE_SEC,
  };
  const body = b64urlEncode(utf8(JSON.stringify(payload)));
  const sig = await hmacSign(body);
  return `${body}.${sig}`;
}

export async function unsealAppSession(token: string | undefined | null): Promise<AppSession | null> {
  if (!token || !token.includes(".")) return null;
  const [body, sig] = token.split(".", 2);
  if (!body || !sig) return null;
  if (!(await hmacVerify(body, sig))) return null;
  try {
    const raw = new TextDecoder().decode(b64urlDecode(body));
    const parsed = JSON.parse(raw) as AppSession;
    if (!parsed?.email || typeof parsed.isAdmin !== "boolean") return null;
    if (typeof parsed.exp !== "number" || parsed.exp < Math.floor(Date.now() / 1000)) return null;
    return {
      email: String(parsed.email).trim().toLowerCase(),
      isAdmin: Boolean(parsed.isAdmin),
      exp: parsed.exp,
    };
  } catch {
    return null;
  }
}

export function backendApiBaseForServer(): string {
  const internal = (process.env.API_INTERNAL_URL || "").replace(/\/+$/, "");
  if (internal) return internal;
  const pub = (process.env.NEXT_PUBLIC_API_URL || "").replace(/\/+$/, "");
  if (pub) return pub;
  return (process.env.NEXT_PUBLIC_API_URL_LOCAL || "http://127.0.0.1:8000").replace(/\/+$/, "");
}
