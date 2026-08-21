import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { APP_SESSION_COOKIE, unsealAppSession } from "@/lib/session-cookie";

/** SSR gate — middleware is primary; this catches missing/stale cookies on render. */
export default async function AdminLayout({ children }: { children: React.ReactNode }) {
  const session = await unsealAppSession(cookies().get(APP_SESSION_COOKIE)?.value);
  if (!session?.isAdmin) {
    redirect("/");
  }
  return <>{children}</>;
}
