/** Resolve a Location header against the upstream request URL. */
export function resolveRedirectLocation(
  location: string | null | undefined,
  upstreamUrl: string
): string | null {
  const raw = (location || "").trim();
  if (!raw) return null;
  try {
    return new URL(raw, upstreamUrl).toString();
  } catch {
    return null;
  }
}

export const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);
