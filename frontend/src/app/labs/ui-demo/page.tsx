import type { Metadata } from "next";
import Link from "next/link";
import { DemoShell } from "@/components/ui-demo/demo-shell";
import { IndexingLanesDemo } from "@/components/ui-demo/indexing-lanes";
import { FoldersSummaryDemo } from "@/components/ui-demo/folders-summary";
import { SearchCarouselHeroDemo } from "@/components/ui-demo/search-carousel-hero";
import { LeadersStripDemo } from "@/components/ui-demo/leaders-strip";

export const metadata: Metadata = {
  title: "UI demo · Masters' Union × DFI",
  description: "Private DriveFaceIndexer redesign using Masters' Union design system",
  robots: { index: false, follow: false },
};

const REFERENCES = [
  { label: "mastersunion.org", href: "https://mastersunion.org" },
  { label: "MU design tokens", href: "/labs/ui-demo" },
  { label: "Montserrat (Galano fallback)", href: "https://fonts.google.com/specimen/Montserrat" },
];

export default function UiDemoPage() {
  return (
    <DemoShell>
      <div className="demo-stack">
        <header className="demo-rise max-w-4xl">
          <p className="demo-eyebrow mb-5">
            Labs · Masters&apos; Union system
            <span className="ml-2 normal-case tracking-normal text-[var(--mu-n950)]">/labs/ui-demo</span>
          </p>
          <h1 className="font-demo-display text-[clamp(2.75rem,8vw,4.5rem)] text-[var(--mu-n950)]">
            Learn media by{" "}
            <span className="demo-scribble">indexing</span> it
          </h1>
          <p className="mt-5 max-w-xl text-lg font-medium leading-relaxed text-[var(--mu-n600)]">
            DriveFaceIndexer surfaces restyled with the exact Masters&apos; Union marketing design
            system — sky, orange, yellow on white, Montserrat / Galano stack.
          </p>
          <div className="mt-8 flex flex-wrap gap-3">
            <Link href="/settings" className="demo-btn demo-btn-ghost">
              ← Settings
            </Link>
            <Link href="/" className="demo-btn demo-btn-lime">
              Open app
            </Link>
            <span className="demo-btn demo-btn-frost pointer-events-none">Mock only · no API</span>
          </div>
        </header>

        <IndexingLanesDemo />
        <FoldersSummaryDemo />
        <SearchCarouselHeroDemo />
        <LeadersStripDemo />

        <section className="demo-rise demo-rise-d6 demo-card-ink">
          <p className="demo-eyebrow">Ready when you are</p>
          <h3 className="mt-2 max-w-lg text-2xl font-bold tracking-tight md:text-3xl">
            Open the real app surfaces
          </h3>
          <p className="mt-3 max-w-md text-sm text-[var(--mu-n400)]">
            This page is a private style lab using MU brand tokens. Jump back into live indexing,
            search, and reverse face.
          </p>
          <div className="mt-6 flex flex-wrap gap-3">
            <Link href="/" className="demo-btn demo-btn-lime">
              Open app
            </Link>
            <Link href="/labs/image-search" className="demo-btn demo-btn-frost">
              Image search
            </Link>
            <Link href="/labs/reverse-face" className="demo-btn demo-btn-ghost border-[var(--mu-n400)] text-white">
              Reverse face
            </Link>
          </div>
        </section>

        <footer className="border-t border-[var(--mu-n200)] pt-8">
          <p className="demo-eyebrow">Brand inspiration</p>
          <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-2 text-xs text-[var(--mu-n600)]">
            {REFERENCES.map((item) => (
              <li key={item.label}>
                <a
                  href={item.href}
                  target={item.href.startsWith("http") ? "_blank" : undefined}
                  rel={item.href.startsWith("http") ? "noopener noreferrer" : undefined}
                  className="underline-offset-2 transition hover:text-[var(--mu-n950)] hover:underline"
                >
                  {item.label}
                </a>
              </li>
            ))}
          </ul>
          <p className="mt-4 text-[11px] text-[var(--mu-n500)]">
            Tokens from{" "}
            <code className="text-[var(--mu-n950)]">docs/mu-design-tokens.md</code> · accents{" "}
            <span className="font-semibold text-[var(--mu-sky)]">#39B6D8</span>{" "}
            <span className="font-semibold text-[var(--mu-orange)]">#E38330</span>{" "}
            <span className="font-semibold text-[var(--mu-n950)]">#F7D344</span>
          </p>
        </footer>
      </div>
    </DemoShell>
  );
}
