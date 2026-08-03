/**
 * Same-origin API proxy for carousel studio.
 *
 * Next.js config `rewrites()` to an upstream die at ~30s with a bare 500, which
 * kills long carousel extract/generate calls while the backend keeps working.
 * This Route Handler waits for the upstream response with no short proxy timeout.
 */
import { NextRequest } from "next/server";
import { proxyToBackend } from "@/lib/api-proxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
/** App-router hint (Vercel / platforms that honor it). Local `next start` waits fully. */
export const maxDuration = 300;

type Ctx = { params: Promise<{ path: string[] }> };

async function handle(req: NextRequest, ctx: Ctx) {
  const { path } = await ctx.params;
  return proxyToBackend(req, path ?? []);
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
export const OPTIONS = handle;
export const HEAD = handle;
