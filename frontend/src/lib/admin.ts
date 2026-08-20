/** Admin allowlist for indexing / backfill controls. */

export function isAdminEmail(email: string | null | undefined): boolean {
  if (!email) return false;
  const raw = process.env.NEXT_PUBLIC_ADMIN_EMAILS ?? "";
  const list = raw
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
  // If unset, treat all authenticated domain users as admin (dev-friendly).
  if (list.length === 0) return true;
  return list.includes(email.trim().toLowerCase());
}
