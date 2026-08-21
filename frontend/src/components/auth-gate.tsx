"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import Script from "next/script";
import { LoadingLabel } from "@/components/spinner";
import { clearAllDataCache } from "@/lib/data-cache";

const ALLOWED_DOMAIN = "mastersunion.org";
const STORAGE_KEY = "dfi_auth";
const LEGACY_STORAGE_KEY = "dfi_auth_email";
/** Keep sign-in across browser restarts (90 days). */
const AUTH_MAX_AGE_MS = 90 * 24 * 60 * 60 * 1000;

const CLIENT_ID = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID ?? "";

type AuthRecord = { email: string; at: number };

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
        writeStoredAuth(legacy);
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

function writeStoredAuth(email: string) {
  const rec: AuthRecord = { email, at: Date.now() };
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

export function getAuthEmail(): string | null {
  return cachedEmail ?? readStoredAuth();
}

export function getIsAdmin(): boolean {
  return cachedIsAdmin;
}

export function useAuthSession(): AuthContextValue {
  return useContext(AuthSessionContext);
}

/** Best-effort: set httpOnly cookie used only by /admin middleware. */
async function syncAdminCookie(credential: string): Promise<boolean> {
  try {
    const res = await fetch("/api/auth/session", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ credential }),
    });
    if (!res.ok) return false;
    const data = (await res.json()) as { isAdmin?: boolean };
    return Boolean(data.isAdmin);
  } catch {
    return false;
  }
}

async function fetchAdminFlag(): Promise<boolean> {
  try {
    const res = await fetch("/api/auth/session", {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!res.ok) return false;
    const data = (await res.json()) as { isAdmin?: boolean };
    return Boolean(data.isAdmin);
  } catch {
    return false;
  }
}

export async function signOut() {
  try {
    await fetch("/api/auth/session", { method: "DELETE", credentials: "same-origin" });
  } catch {
    /* ignore — app logout still clears localStorage */
  }
  clearStoredAuth();
  clearAllDataCache();
  cachedEmail = null;
  cachedIsAdmin = false;
  window.location.reload();
}

export function AuthGate({ children }: { children: ReactNode }) {
  const [email, setEmail] = useState<string | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [gsiReady, setGsiReady] = useState(false);
  const [mounted, setMounted] = useState(false);

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

  useEffect(() => {
    setMounted(true);
    const stored = readStoredAuth();
    if (stored) {
      applyEmail(stored, false);
      void fetchAdminFlag().then((admin) => {
        cachedIsAdmin = admin;
        setIsAdmin(admin);
      });
    }
  }, [applyEmail]);

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
        writeStoredAuth(userEmail);
        applyEmail(userEmail, false);
        setError(null);
        void syncAdminCookie(response.credential).then((admin) => {
          cachedIsAdmin = admin;
          setIsAdmin(admin);
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
    return <AuthSessionContext.Provider value={ctx}>{children}</AuthSessionContext.Provider>;
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

        <p className="text-xs text-muted-foreground">
          Only <span className="font-medium">@{ALLOWED_DOMAIN}</span> emails are allowed access.
        </p>
      </div>
    </div>
  );
}
