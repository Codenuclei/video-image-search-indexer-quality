/** Safe localStorage helpers with size guard for dfi cache payloads. */

const SOFT_MAX_BYTES = 3.5 * 1024 * 1024; // ~3.5MB soft cap per write

export function localGet(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function localSet(key: string, value: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    if (value.length > SOFT_MAX_BYTES) return false;
    localStorage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

export function localRemove(key: string): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

export function localRemovePrefix(prefix: string): void {
  if (typeof window === "undefined") return;
  try {
    const keys: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k?.startsWith(prefix)) keys.push(k);
    }
    for (const k of keys) localStorage.removeItem(k);
  } catch {
    /* ignore */
  }
}

export function localGetJson<T>(key: string): T | null {
  const raw = localGet(key);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    localRemove(key);
    return null;
  }
}

export function localSetJson(key: string, value: unknown): boolean {
  try {
    return localSet(key, JSON.stringify(value));
  } catch {
    return false;
  }
}
