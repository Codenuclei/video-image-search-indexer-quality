import { Montserrat } from "next/font/google";
import type { ReactNode } from "react";

/** Galano Grotesque is brand-primary but unlicensed here — Montserrat is the official public fallback. */
const montserrat = Montserrat({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700", "800"],
  variable: "--font-demo-ui",
  display: "swap",
});

export default function UiDemoLayout({ children }: { children: ReactNode }) {
  return (
    <div
      className={montserrat.variable}
      style={{
        fontFamily: "var(--font-demo-ui), 'Montserrat', system-ui, -apple-system, 'Segoe UI', sans-serif",
      }}
    >
      <style>{`
        .font-demo-display {
          font-family: var(--font-demo-ui), 'Montserrat', system-ui, sans-serif;
          font-weight: 800;
          letter-spacing: -0.03em;
          line-height: 0.96;
        }
      `}</style>
      {children}
    </div>
  );
}
