import Navbar from "@/components/Navbar";
import Hero from "@/components/Hero";
import { GradientBackground } from "@/components/ui/oceanic-shimmer";

const STEPS = [
  {
    n: "01",
    title: "Choose a video",
    body: "Browse recent captioned videos or search your library.",
  },
  {
    n: "02",
    title: "Pick themes & hooks",
    body: "Surface narrative beats and select the lines you want to feature.",
  },
  {
    n: "03",
    title: "Generate slides",
    body: "Build Instagram-ready cards, swap frames, and polish copy.",
  },
] as const;

export default function Home() {
  return (
    <main className="min-h-screen bg-white overflow-x-hidden">
      <Navbar />
      <Hero />
      <section
        id="how-it-works"
        className="relative z-10 -mt-1 min-h-[640px] overflow-hidden border-t border-[#e5e5e5] px-4 py-20 sm:min-h-[720px] sm:px-6 sm:py-28"
      >
        {/* Absolute fill wrapper — GradientBackground root stays position:relative + h/w 100% */}
        <div className="pointer-events-none absolute inset-0" aria-hidden>
          <GradientBackground className="h-full w-full" />
        </div>
        {/* Soft veil for readable light type on the blue shimmer */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 bg-gradient-to-b from-[#0a2444]/25 via-transparent to-[#0a2444]/40"
        />

        <div className="relative z-10 mx-auto flex min-h-[560px] max-w-3xl flex-col justify-center text-center sm:min-h-[640px]">
          <p className="text-[11px] font-medium uppercase tracking-[0.22em] text-white/75">
            How it works
          </p>
          <h2 className="mt-3 font-serif text-3xl tracking-tight text-white sm:text-4xl md:text-[2.75rem] md:leading-tight">
            Three steps to a finished carousel
          </h2>
          <p className="mx-auto mt-4 max-w-xl text-sm leading-relaxed text-white/85 sm:text-base">
            Choose a captioned video, pick the themes and hooks that matter, then generate
            slide layouts with frames you can refine before export.
          </p>

          <ol className="mt-12 grid gap-4 text-left sm:grid-cols-3 sm:gap-5">
            {STEPS.map((step) => (
              <li
                key={step.n}
                className="rounded-2xl border border-white/55 bg-white/92 px-5 py-6 shadow-[0_12px_40px_-18px_rgba(10,36,68,0.5)] backdrop-blur-md"
              >
                <p className="text-xs font-medium tracking-wide text-[#123A6B]/55">
                  {step.n}
                </p>
                <p className="mt-2.5 font-medium text-[#0a2444]">{step.title}</p>
                <p className="mt-1.5 text-sm leading-relaxed text-[#0a2444]/70">
                  {step.body}
                </p>
              </li>
            ))}
          </ol>

          <a
            href="/carousel"
            className="mt-12 inline-flex self-center rounded-lg bg-white px-6 py-3 text-sm font-medium text-[#0a2444] shadow-sm transition-colors hover:bg-white/90"
          >
            Open studio
          </a>
        </div>
      </section>
    </main>
  );
}
