"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import Script from "next/script";
import { clearSavedFileIdsCache } from "@/components/add-to-collection";
import { LoadingLabel } from "@/components/spinner";
import {
  ensureAppSession,
  notifySessionRestored,
  REAUTH_NEEDED_EVENT,
} from "@/lib/app-session";
import { clearAllDataCache } from "@/lib/data-cache";

export { requestReauth } from "@/lib/app-session";

const ALLOWED_DOMAIN = "mastersunion.org";
const STORAGE_KEY = "dfi_auth";
const LEGACY_STORAGE_KEY = "dfi_auth_email";
/** Keep sign-in across browser restarts (90 days). */
const AUTH_MAX_AGE_MS = 90 * 24 * 60 * 60 * 1000;

const CLIENT_ID = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID ?? "";

type AuthMethod = "google" | "demo" | "admin";
type AuthRecord = { email: string; at: number; method?: AuthMethod };

type ReauthResult =
  | { ok: true }
  | { ok: false; reason: "invalid" | "mismatch" | "domain" | "server" };

type AuthContextValue = {
  email: string | null;
  isAdmin: boolean;
  refreshAdmin: () => Promise<void>;
};

const AuthSessionContext = createContext<AuthContextValue>({
  email: null,
  isAdmin: false,
  refreshAdmin: async () => {},
});

let cachedEmail: string | null = null;
let cachedIsAdmin = false;

function readStoredAuth(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      const legacy = sessionStorage.getItem(LEGACY_STORAGE_KEY);
      if (legacy) {
        writeStoredAuth(legacy, "google");
        sessionStorage.removeItem(LEGACY_STORAGE_KEY);
        return legacy;
      }
      return null;
    }
    const rec = JSON.parse(raw) as AuthRecord;
    if (!rec.email || Date.now() - (rec.at ?? 0) > AUTH_MAX_AGE_MS) {
      localStorage.removeItem(STORAGE_KEY);
      return null;
    }
    return rec.email;
  } catch {
    localStorage.removeItem(STORAGE_KEY);
    return null;
  }
}

function readStoredMethod(): AuthMethod {
  if (typeof window === "undefined") return "google";
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return "google";
    const rec = JSON.parse(raw) as AuthRecord;
    if (rec.method === "demo" || rec.method === "admin" || rec.method === "google") {
      return rec.method;
    }
    return "google";
  } catch {
    return "google";
  }
}

function writeStoredAuth(email: string, method: AuthMethod) {
  const rec: AuthRecord = { email, at: Date.now(), method };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(rec));
}

function clearStoredAuth() {
  localStorage.removeItem(STORAGE_KEY);
  sessionStorage.removeItem(LEGACY_STORAGE_KEY);
}

function parseJwt(token: string): Record<string, string> | null {
  try {
    const base64 = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(base64));
  } catch {
    return null;
  }
}

function reauthErrorMessage(
  reason: "invalid" | "mismatch" | "domain" | "server",
  storedEmail: string
): string {
  if (reason === "mismatch") {
    return `That Google account does not match this session (signed in as ${storedEmail}). Choose that account.`;
  }
  if (reason === "domain") {
    return `Access is restricted to @${ALLOWED_DOMAIN} accounts only.`;
  }
  return "Sign-in failed, please try again.";
}

export function getAuthEmail(): string | null {
  return cachedEmail ?? readStoredAuth();
}

export function getIsAdmin(): boolean {
  return cachedIsAdmin;
}

export function useAuthSession(): AuthContextValue {
  return useContext(AuthSessionContext);
}

/** Mint/refresh the httpOnly dfi_app_session cookie from a Google ID token. */
async function syncAdminCookie(
  credential: string
): Promise<{ ok: boolean; isAdmin: boolean }> {
  try {
    const res = await fetch("/api/auth/session", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ credential }),
    });
    if (!res.ok) return { ok: false, isAdmin: false };
    const data = (await res.json()) as { isAdmin?: boolean };
    return { ok: true, isAdmin: Boolean(data.isAdmin) };
  } catch {
    return { ok: false, isAdmin: false };
  }
}

async function fetchAdminFlag(): Promise<boolean> {
  const session = await ensureAppSession();
  return session.ok && session.isAdmin;
}

export async function signOut() {
  try {
    await fetch("/api/auth/session", { method: "DELETE", credentials: "same-origin" });
  } catch {
    /* ignore — app logout still clears localStorage */
  }
  clearStoredAuth();
  clearAllDataCache();
  clearSavedFileIdsCache();
  cachedEmail = null;
  cachedIsAdmin = false;
  window.location.reload();
}

type GsiPromptNotification = {
  isNotDisplayed?: () => boolean;
  isSkippedMoment?: () => boolean;
  isDismissedMoment?: () => boolean;
  getDismissedReason?: () => string;
};

export function AuthGate({ children }: { children: ReactNode }) {
  const [email, setEmail] = useState<string | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [gsiReady, setGsiReady] = useState(false);
  const [mounted, setMounted] = useState(false);
  const [adminEmail, setAdminEmail] = useState("");
  const [adminPassword, setAdminPassword] = useState("");
  const [adminLoading, setAdminLoading] = useState(false);
  /** Cookie missing / collections 401 — need sealed session again. */
  const [cookieMissing, setCookieMissing] = useState(false);
  /** Show inline banner after One Tap dismiss/unavailable (or explicit reauth event). */
  const [showReauthBanner, setShowReauthBanner] = useState(false);
  const [reauthError, setReauthError] = useState<string | null>(null);
  const [authMethod, setAuthMethod] = useState<AuthMethod>("google");
  const [reauthPassword, setReauthPassword] = useState("");
  const [reauthPasswordLoading, setReauthPasswordLoading] = useState(false);
  const reauthInitRef = useRef(false);
  const expectedEmailRef = useRef<string | null>(null);
  const probeRetryTimersRef = useRef<ReturnType<typeof setTimeout>[]>([]);

  const applyEmail = useCallback((next: string | null, admin = false) => {
    cachedEmail = next;
    cachedIsAdmin = admin;
    setEmail(next);
    setIsAdmin(admin);
  }, []);

  const refreshAdmin = useCallback(async () => {
    const admin = await fetchAdminFlag();
    cachedIsAdmin = admin;
    setIsAdmin(admin);
  }, []);

  const finishReauth = useCallback(
    async (credential: string, expectedEmail: string): Promise<ReauthResult> => {
      const payload = parseJwt(credential);
      if (!payload) return { ok: false, reason: "invalid" };
      const userEmail = (payload.email ?? "").trim().toLowerCase();
      const expected = expectedEmail.trim().toLowerCase();
      if (!userEmail || userEmail !== expected) {
        return { ok: false, reason: "mismatch" };
      }
      const hd = payload.hd ?? "";
      if (hd !== ALLOWED_DOMAIN && !userEmail.endsWith(`@${ALLOWED_DOMAIN}`)) {
        return { ok: false, reason: "domain" };
      }
      const synced = await syncAdminCookie(credential);
      if (!synced.ok) {
        return { ok: false, reason: "server" };
      }
      cachedIsAdmin = synced.isAdmin;
      setIsAdmin(synced.isAdmin);
      writeStoredAuth(userEmail, "google");
      setAuthMethod("google");
      applyEmail(userEmail, synced.isAdmin);
      setCookieMissing(false);
      setShowReauthBanner(false);
      setReauthError(null);
      reauthInitRef.current = false;
      notifySessionRestored();
      return { ok: true };
    },
    [applyEmail]
  );

  async function submitAdminLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (adminLoading) return;
    setAdminLoading(true);
    setError(null);
    try {
      const response = await fetch("/api/auth/admin", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: adminEmail, password: adminPassword }),
      });
      const data = (await response.json().catch(() => ({}))) as {
        email?: string;
        isAdmin?: boolean;
        detail?: string;
      };
      if (!response.ok || !data.email) {
        setError(data.detail || "Admin sign-in failed.");
        return;
      }
      writeStoredAuth(data.email, "admin");
      setAuthMethod("admin");
      applyEmail(data.email, true);
      setAdminPassword("");
    } catch {
      setError("Admin sign-in is temporarily unavailable.");
    } finally {
      setAdminLoading(false);
    }
  }

  async function submitPasswordReauth(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (reauthPasswordLoading || !email) return;
    const method = authMethod === "admin" ? "admin" : "demo";
    setReauthPasswordLoading(true);
    setReauthError(null);
    try {
      const response = await fetch(`/api/auth/${method}`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password: reauthPassword }),
      });
      const data = (await response.json().catch(() => ({}))) as {
        email?: string;
        isAdmin?: boolean;
        detail?: string;
      };
      if (!response.ok || !data.email) {
        setReauthError(data.detail || "Wrong password. Please try again.");
        return;
      }
      writeStoredAuth(data.email, method);
      setAuthMethod(method);
      applyEmail(data.email, method === "admin" || Boolean(data.isAdmin));
      setReauthPassword("");
      setCookieMissing(false);
      setShowReauthBanner(false);
      setReauthError(null);
      reauthInitRef.current = false;
      notifySessionRestored();
    } catch {
      setReauthError("Sign-in failed, please try again.");
    } finally {
      setReauthPasswordLoading(false);
    }
  }

  useEffect(() => {
    setMounted(true);
    const stored = readStoredAuth();
    if (stored) {
      const method = readStoredMethod();
      setAuthMethod(method);
      applyEmail(stored, false);
      expectedEmailRef.current = stored;

      let cancelled = false;
      const clearProbeTimers = () => {
        for (const t of probeRetryTimersRef.current) clearTimeout(t);
        probeRetryTimersRef.current = [];
      };

      const probe = async (attempt: number) => {
        const session = await ensureAppSession();
        if (cancelled) return;
        if (session.ok && session.email) {
          applyEmail(session.email, session.isAdmin);
          expectedEmailRef.current = session.email;
          // Undo a banner that a Collections 401 may have opened during an unreachable retry.
          setCookieMissing(false);
          setShowReauthBanner(false);
          setReauthError(null);
          reauthInitRef.current = false;
          return;
        }
        if (session.reason === "unauthorized") {
          setCookieMissing(true);
          if (method === "demo" || method === "admin") {
            setShowReauthBanner(true);
          }
          return;
        }
        // unreachable: retry with short backoff, then stop quietly
        if (attempt < 2) {
          const delayMs = attempt === 0 ? 2000 : 5000;
          const timer = setTimeout(() => {
            void probe(attempt + 1);
          }, delayMs);
          probeRetryTimersRef.current.push(timer);
        }
      };

      void probe(0);

      return () => {
        cancelled = true;
        clearProbeTimers();
      };
    }
  }, [applyEmail]);

  // Collections / Save 401 paths: start silent One Tap first; banner only if it already ran.
  useEffect(() => {
    const onReauthNeeded = () => {
      setCookieMissing(true);
      const method = readStoredMethod();
      setAuthMethod(method);
      if (method === "demo" || method === "admin") {
        setShowReauthBanner(true);
        return;
      }
      // Silent attempt already finished (or in flight finished) — surface the banner.
      // First paint / hydrator 401s leave the banner off so auto_select can run.
      if (reauthInitRef.current) {
        setShowReauthBanner(true);
      } else {
        // Reset so the silent effect can initialize One Tap.
        reauthInitRef.current = false;
      }
    };
    window.addEventListener(REAUTH_NEEDED_EVENT, onReauthNeeded);
    return () => window.removeEventListener(REAUTH_NEEDED_EVENT, onReauthNeeded);
  }, []);

  // Fresh sign-in (no stored email).
  useEffect(() => {
    if (!gsiReady || !mounted || email) return;

    (window as any).google.accounts.id.initialize({
      client_id: CLIENT_ID,
      callback: (response: { credential: string }) => {
        const payload = parseJwt(response.credential);
        if (!payload) {
          setError("Invalid sign-in response. Please try again.");
          return;
        }
        const userEmail = payload.email ?? "";
        const hd = payload.hd ?? "";
        if (hd !== ALLOWED_DOMAIN && !userEmail.endsWith(`@${ALLOWED_DOMAIN}`)) {
          setError(`Access is restricted to @${ALLOWED_DOMAIN} accounts only.`);
          return;
        }
        // Existing app gate: localStorage. Cookie is only for /admin middleware.
        writeStoredAuth(userEmail, "google");
        setAuthMethod("google");
        applyEmail(userEmail, false);
        expectedEmailRef.current = userEmail;
        setError(null);
        void syncAdminCookie(response.credential).then((synced) => {
          if (!synced.ok) {
            setError("Sign-in failed, please try again.");
            return;
          }
          cachedIsAdmin = synced.isAdmin;
          setIsAdmin(synced.isAdmin);
        });
      },
      hd: ALLOWED_DOMAIN,
    });

    const btn = document.getElementById("gsi-button");
    if (btn) {
      (window as any).google.accounts.id.renderButton(btn, {
        theme: "outline",
        size: "large",
        width: 280,
        text: "signin_with",
        shape: "rectangular",
      });
    }
    (window as any).google.accounts.id.prompt();
  }, [gsiReady, mounted, email, applyEmail]);

  // Silent re-auth when stored email exists but sealed cookie is missing (Google only).
  useEffect(() => {
    if (!gsiReady || !mounted || !email || !cookieMissing) return;
    if (authMethod !== "google") return;
    if (reauthInitRef.current) return;
    reauthInitRef.current = true;
    const expected = expectedEmailRef.current ?? email;

    (window as any).google.accounts.id.initialize({
      client_id: CLIENT_ID,
      auto_select: true,
      callback: (response: { credential: string }) => {
        setReauthError(null);
        void finishReauth(response.credential, expected).then((result) => {
          if (result.ok) return;
          setReauthError(reauthErrorMessage(result.reason, expected));
          setShowReauthBanner(true);
        });
      },
      hd: ALLOWED_DOMAIN,
    });

    (window as any).google.accounts.id.prompt((notification: GsiPromptNotification) => {
      // isDismissedMoment also fires on credential_returned — do not flash the banner then.
      const dismissedReason =
        typeof notification.getDismissedReason === "function"
          ? notification.getDismissedReason()
          : "";
      const unavailable =
        Boolean(notification.isNotDisplayed?.()) ||
        Boolean(notification.isSkippedMoment?.()) ||
        (Boolean(notification.isDismissedMoment?.()) &&
          dismissedReason !== "credential_returned");
      if (unavailable) setShowReauthBanner(true);
    });
  }, [gsiReady, mounted, email, cookieMissing, authMethod, finishReauth]);

  // Banner Google button when One Tap was dismissed / event-driven reauth.
  useEffect(() => {
    if (!gsiReady || !showReauthBanner || !email) return;
    if (authMethod !== "google") return;
    const expected = expectedEmailRef.current ?? email;
    (window as any).google.accounts.id.initialize({
      client_id: CLIENT_ID,
      callback: (response: { credential: string }) => {
        setReauthError(null);
        void finishReauth(response.credential, expected).then((result) => {
          if (result.ok) return;
          setReauthError(reauthErrorMessage(result.reason, expected));
        });
      },
      hd: ALLOWED_DOMAIN,
    });
    const btn = document.getElementById("gsi-reauth-button");
    if (btn) {
      btn.innerHTML = "";
      (window as any).google.accounts.id.renderButton(btn, {
        theme: "outline",
        size: "medium",
        text: "signin_with",
        shape: "rectangular",
      });
    }
  }, [gsiReady, showReauthBanner, email, authMethod, finishReauth]);

  const ctx: AuthContextValue = {
    email,
    isAdmin,
    refreshAdmin,
  };

  if (!mounted) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <LoadingLabel size={18} className="text-muted-foreground">
          Loading…
        </LoadingLabel>
      </div>
    );
  }

  if (email) {
    const needsGoogleGsi =
      authMethod === "google" && (cookieMissing || showReauthBanner);
    const passwordReauth = authMethod === "demo" || authMethod === "admin";

    return (
      <AuthSessionContext.Provider value={ctx}>
        {needsGoogleGsi && (
          <Script
            src="https://accounts.google.com/gsi/client"
            strategy="afterInteractive"
            onLoad={() => setGsiReady(true)}
          />
        )}
        {showReauthBanner && (
          <div
            role="status"
            className="sticky top-0 z-50 flex flex-wrap items-center justify-center gap-3 border-b border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-950 dark:text-amber-100"
          >
            {passwordReauth ? (
              <form
                className="flex flex-wrap items-center justify-center gap-2"
                onSubmit={submitPasswordReauth}
              >
                <span>Your session expired — enter your password to continue</span>
                <span className="font-medium">{email}</span>
                <input
                  type="password"
                  autoComplete="current-password"
                  value={reauthPassword}
                  onChange={(event) => setReauthPassword(event.target.value)}
                  required
                  placeholder="Password"
                  className="h-8 w-40 rounded-md border border-amber-600/40 bg-background px-2 text-sm text-foreground outline-none focus:border-ring focus:ring-2 focus:ring-ring/20"
                />
                <button
                  type="submit"
                  disabled={reauthPasswordLoading}
                  className="flex h-8 items-center justify-center rounded-md bg-amber-700 px-3 text-xs font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-60 dark:bg-amber-600"
                >
                  {reauthPasswordLoading ? (
                    <LoadingLabel size={12}>Signing in…</LoadingLabel>
                  ) : (
                    "Continue"
                  )}
                </button>
                {reauthError && (
                  <span className="w-full text-center text-xs text-destructive sm:w-auto">
                    {reauthError}
                  </span>
                )}
              </form>
            ) : (
              <>
                <span>Sign in again to use Collections and Save</span>
                <div id="gsi-reauth-button" className="flex items-center" />
                {reauthError && (
                  <span className="w-full text-center text-xs text-destructive sm:w-auto">
                    {reauthError}
                  </span>
                )}
              </>
            )}
          </div>
        )}
        {children}
      </AuthSessionContext.Provider>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <Script
        src="https://accounts.google.com/gsi/client"
        strategy="afterInteractive"
        onLoad={() => setGsiReady(true)}
      />

      <div className="flex w-full max-w-sm flex-col items-center gap-6 rounded-2xl border border-border bg-card p-6 shadow-md text-center sm:p-10">
        <div className="flex flex-col items-center gap-3">
          <div className="inline-flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-amber-400 to-orange-500 shadow">
            <span className="text-xl font-bold text-white">DFI</span>
          </div>
          <div>
            <h1 className="text-xl font-semibold text-foreground">DriveFaceIndexer</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              Sign in with your <span className="font-medium">@{ALLOWED_DOMAIN}</span> account to continue.
            </p>
          </div>
        </div>

        {error && (
          <div className="w-full rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
            {error}
          </div>
        )}

        {!gsiReady && (
          <p className="text-sm text-muted-foreground">
            <LoadingLabel size={14}>Loading sign-in…</LoadingLabel>
          </p>
        )}
        <div id="gsi-button" className="flex justify-center" />

        <div className="flex w-full items-center gap-3 text-xs text-muted-foreground">
          <span className="h-px flex-1 bg-border" />
          <span>or admin sign-in</span>
          <span className="h-px flex-1 bg-border" />
        </div>

        <form className="w-full space-y-3 text-left" onSubmit={submitAdminLogin}>
          <label className="block space-y-1">
            <span className="text-xs font-medium text-foreground">Admin email</span>
            <input
              type="email"
              autoComplete="username"
              value={adminEmail}
              onChange={(event) => setAdminEmail(event.target.value)}
              required
              className="h-10 w-full rounded-lg border border-input bg-background px-3 text-sm outline-none focus:border-ring focus:ring-2 focus:ring-ring/20"
            />
          </label>
          <label className="block space-y-1">
            <span className="text-xs font-medium text-foreground">Password</span>
            <input
              type="password"
              autoComplete="current-password"
              value={adminPassword}
              onChange={(event) => setAdminPassword(event.target.value)}
              required
              className="h-10 w-full rounded-lg border border-input bg-background px-3 text-sm outline-none focus:border-ring focus:ring-2 focus:ring-ring/20"
            />
          </label>
          <button
            type="submit"
            disabled={adminLoading}
            className="flex h-10 w-full items-center justify-center rounded-lg border border-border bg-secondary px-4 text-sm font-medium text-secondary-foreground transition-opacity disabled:cursor-not-allowed disabled:opacity-60"
          >
            {adminLoading ? <LoadingLabel size={14}>Signing in…</LoadingLabel> : "Admin sign-in"}
          </button>
        </form>

        <p className="text-xs text-muted-foreground">
          Google access is limited to <span className="font-medium">@{ALLOWED_DOMAIN}</span> accounts.
        </p>
      </div>
    </div>
  );
}
