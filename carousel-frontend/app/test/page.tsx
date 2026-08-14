import Link from "next/link";
import { TestLandingLlmControl } from "./test-landing-llm";

export default function TestLandingPage() {
  return (
    <section className="mx-auto flex min-h-[calc(100svh-4rem)] max-w-3xl flex-col items-center justify-center px-4 py-16 text-center sm:px-6">
      <p className="text-[11px] font-medium uppercase tracking-[0.2em] text-slate-500">
        Local test flow
      </p>
      <h1 className="mt-3 font-serif text-4xl tracking-tight text-slate-900 sm:text-5xl md:text-6xl">
        Carousel Studio
      </h1>
      <p className="mt-4 max-w-md text-sm leading-relaxed text-slate-600 sm:text-base">
        Redesigned flow demo — create a project or open an existing one. Hits the real backend
        via <code className="rounded bg-slate-100 px-1.5 py-0.5 text-xs">/api/proxy</code>{" "}
        (same as production studio).
      </p>
      <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
        <Link
          href="/test/studio"
          className="rounded-lg bg-slate-900 px-6 py-3 text-sm font-medium text-white transition-colors hover:bg-slate-800 sm:px-8 sm:py-3.5"
          data-testid="test-create-project"
        >
          Create Project
        </Link>
        <Link
          href="/test/library"
          className="rounded-lg border border-slate-300 bg-white/90 px-6 py-3 text-sm font-medium text-slate-900 transition-colors hover:bg-white sm:px-8 sm:py-3.5"
          data-testid="test-existing-project"
        >
          Add Existing Project
        </Link>
      </div>
      <TestLandingLlmControl />
      <p className="mt-10 max-w-sm text-xs text-slate-400">
        Production landing stays at{" "}
        <Link href="/" className="underline underline-offset-2 hover:text-slate-600">
          /
        </Link>
        . Production studio at{" "}
        <Link href="/carousel" className="underline underline-offset-2 hover:text-slate-600">
          /carousel
        </Link>
        .
      </p>
    </section>
  );
}
