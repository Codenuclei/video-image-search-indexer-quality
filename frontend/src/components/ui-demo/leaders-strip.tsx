"use client";

const LEADERS = [
  { name: "A. Mehta", role: "CEO", score: 94 },
  { name: "J. Okonkwo", role: "CTO", score: 91 },
  { name: "S. Park", role: "CFO", score: 88 },
  { name: "M. Alvarez", role: "CHRO", score: 86 },
  { name: "R. Singh", role: "CMO", score: 84 },
  { name: "L. Chen", role: "GC", score: 82 },
  { name: "N. Brooks", role: "COO", score: 80 },
  { name: "E. Vargas", role: "CPO", score: 78 },
];

const AVATAR = [
  "bg-[var(--mu-sky)] text-white",
  "bg-[var(--mu-orange)] text-white",
  "bg-[var(--mu-yellow)] text-[var(--mu-n950)]",
  "bg-[var(--mu-n950)] text-white",
];

export function LeadersStripDemo() {
  const loop = [...LEADERS, ...LEADERS];

  return (
    <section className="demo-rise demo-rise-d5 overflow-hidden">
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="demo-eyebrow">Reverse face · leaders</p>
          <h3 className="demo-section-title">Roster strip</h3>
        </div>
        <p className="text-xs font-medium text-[var(--mu-n600)]">Match confidence · mock crawl</p>
      </div>

      <div className="relative -mx-1 overflow-hidden rounded-[12px] border border-[var(--mu-n200)] bg-[var(--mu-n50)] py-4">
        <div className="pointer-events-none absolute inset-y-0 left-0 z-10 w-12 bg-gradient-to-r from-[var(--mu-n50)] to-transparent" />
        <div className="pointer-events-none absolute inset-y-0 right-0 z-10 w-12 bg-gradient-to-l from-[var(--mu-n50)] to-transparent" />
        <div className="demo-marquee-track flex w-max gap-3 px-4">
          {loop.map((p, i) => (
            <article
              key={`${p.name}-${i}`}
              className="flex w-44 shrink-0 items-center gap-3 rounded-[12px] border border-[var(--mu-n200)] bg-white px-3 py-2.5"
            >
              <div
                className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-sm font-bold ${
                  AVATAR[i % AVATAR.length]
                }`}
              >
                {p.name
                  .split(" ")
                  .map((n) => n[0])
                  .join("")}
              </div>
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold">{p.name}</p>
                <p className="text-[11px] font-medium text-[var(--mu-n500)]">
                  {p.role} · {p.score}%
                </p>
              </div>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
