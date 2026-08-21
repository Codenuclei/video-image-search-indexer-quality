import { NextResponse, type NextRequest } from "next/server";
import { APP_SESSION_COOKIE, unsealAppSession } from "@/lib/session-cookie";

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (!pathname.startsWith("/admin")) {
    return NextResponse.next();
  }

  const token = request.cookies.get(APP_SESSION_COOKIE)?.value;
  const session = await unsealAppSession(token);
  if (!session?.isAdmin) {
    const url = request.nextUrl.clone();
    url.pathname = "/";
    url.search = "";
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/admin/:path*"],
};
