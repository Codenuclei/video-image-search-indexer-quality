/** Admin access comes from DB ``app_admins`` via the signed httpOnly session cookie. */

import { getIsAdmin } from "@/components/auth-gate";

/** Client helper — prefer ``useAuthSession().isAdmin`` in React trees. */
export function isAdminEmail(_email?: string | null): boolean {
  return getIsAdmin();
}
