import type { ReactNode } from "react";
import Link from "next/link";
import StudioLogo from "@/components/StudioLogo";
import "./carousel-studio.css";

export default function CarouselStudioLayout({ children }: { children: ReactNode }) {
  return (
    <div className="relative min-h-screen overflow-x-hidden text-slate-900">
      <div className="absolute inset-0 -z-10 size-full bg-white [background:radial-gradient(125%_125%_at_50%_10%,#f8fafc_35%,#e2e8f0_55%,#1e293b_100%)]" />
      <nav className="sticky top-0 z-50 border-b border-slate-200 bg-white shadow-sm">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3.5 sm:px-6">
          <Link href="/" className="flex items-center gap-2.5 text-slate-900">
            <StudioLogo className="h-5 w-5" />
            <span className="text-sm font-semibold tracking-tight">Carousel Studio</span>
          </Link>
          <div className="flex items-center gap-3">
            <Link
              href="/library"
              className="hidden text-sm text-slate-500 transition-colors hover:text-slate-900 sm:inline"
            >
              Library
            </Link>
            {/* Current-page CTA: not a link — <a href="/carousel"> full-reloads the studio. */}
            <span
              className="inline-flex h-9 items-center rounded-lg border border-slate-900 bg-slate-900 px-4 text-sm font-medium text-white shadow-sm"
              aria-current="page"
            >
              Studio
            </span>
          </div>
        </div>
      </nav>
      <main className="carousel-studio relative z-10 px-4 pb-24 pt-8 sm:px-6 sm:pt-10">
        {children}
      </main>
    </div>
  );
}
