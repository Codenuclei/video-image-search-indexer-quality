"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import StudioLogo from "@/components/StudioLogo";
import "../carousel/carousel-studio.css";
import "./test-studio.css";

const ACTIVE_TAB =
  "inline-flex h-9 items-center rounded-lg border border-slate-900 bg-slate-900 px-4 text-sm font-medium text-white shadow-sm";
const INACTIVE_TAB = "text-sm text-slate-500 transition-colors hover:text-slate-900";

function TestNav() {
  const pathname = usePathname() ?? "";
  const libraryActive = pathname === "/test/library" || pathname.startsWith("/test/library/");

  return (
    <nav className="sticky top-0 z-50 border-b border-slate-200 bg-white/95 shadow-sm backdrop-blur">
      <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3.5 sm:px-6">
        <div className="flex items-center gap-3">
          <Link href="/test" className="flex items-center gap-2.5 text-slate-900">
            <StudioLogo className="h-5 w-5" />
            <span className="text-sm font-semibold tracking-tight">Carousel Studio</span>
            <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-amber-800">
              Test
            </span>
          </Link>
          <Link
            href="/carousel"
            className="hidden rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-medium text-slate-500 transition-colors hover:border-slate-300 hover:text-slate-800 sm:inline"
            title="Open the production Carousel Studio"
          >
            Prod studio
          </Link>
        </div>
        <div className="flex items-center gap-3">
          {libraryActive ? (
            <>
              <Link
                href="/test/library"
                aria-current="page"
                className={`hidden sm:inline-flex ${ACTIVE_TAB}`}
              >
                Library
              </Link>
              <Link href="/test/studio" className={INACTIVE_TAB}>
                Studio
              </Link>
            </>
          ) : (
            <>
              <Link href="/test/library" className={`hidden sm:inline ${INACTIVE_TAB}`}>
                Library
              </Link>
              <Link href="/test/studio" aria-current="page" className={ACTIVE_TAB}>
                Studio
              </Link>
            </>
          )}
        </div>
      </div>
    </nav>
  );
}

export default function TestLayout({ children }: { children: ReactNode }) {
  return (
    <div className="relative min-h-screen overflow-x-hidden text-slate-900">
      <div className="absolute inset-0 -z-10 size-full bg-white [background:radial-gradient(125%_125%_at_50%_10%,#f8fafc_35%,#e2e8f0_55%,#1e293b_100%)]" />
      <TestNav />
      <main className="carousel-studio relative z-10">{children}</main>
    </div>
  );
}
