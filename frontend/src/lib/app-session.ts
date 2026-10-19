/**
 * Same-origin httpOnly app session helpers (dfi_app_session cookie).
 * Used before MU People / collections BFFs so localStorage-only auth
 * is not confused with a missing sealed cookie.
 */

/** Fired when collections/Save need a sealed cookie again (localStorage-only auth). */
export const REAUTH_NEEDED_EVENT = "dfi:reauth-needed";

/** Fired after a sealed cookie is successfully re-minted (silent or banner sign-in). */
export const SESSION_RESTORED_EVENT = "dfi:session-restored";

/**
 * Ask AuthGate to start silent One Tap (banner only if One Tap is unavailable).
 * Prefer this over forcing the banner so cookie-less reloads do not flash UI first.
 */
export function requestReauth(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(REAUTH_NEEDED_EVENT));
}

/** Notify listeners that Save/Collections can retry after re-auth. */
export function notifySessionRestored(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(SESSION_RESTORED_EVENT));
}

export type AppSessionSnapshot = {
  email: string | null;
  isAdmin: boolean;
  ok: boolean;
  reason: "ok" | "unauthorized" | "unreachable";
};

/**
 * Refresh / probe the sealed session cookie.
 * Returns ok=false on HTTP 401 (unauthorized) or network/5xx (unreachable) — never toasts.
 */
export async function ensureAppSession(): Promise<AppSessionSnapshot> {
  try {
    const res = await fetch("/api/auth/session", {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
    });
    if (res.status === 401) {
      return { email: null, isAdmin: false, ok: false, reason: "unauthorized" };
    }
    if (!res.ok) {
      return { email: null, isAdmin: false, ok: false, reason: "unreachable" };
    }
    const data = (await res.json()) as { email?: string | null; isAdmin?: boolean };
    return {
      email: data.email ?? null,
      isAdmin: Boolean(data.isAdmin),
      ok: true,
      reason: "ok",
    };
  } catch {
    return { email: null, isAdmin: false, ok: false, reason: "unreachable" };
  }
}
