"use client";

import type { ReactNode } from "react";
import "./demo-styles.css";

export function DemoShell({ children }: { children: ReactNode }) {
  return (
    <div className="dfi-ui-demo relative -mx-4 -mt-[calc(3.5rem+env(safe-area-inset-top))] min-h-[100dvh] pb-20 md:-m-8 md:min-h-full md:rounded-[16px] md:border md:border-[var(--mu-n200)]">
      <div className="demo-container relative z-10 px-5 pb-12 pt-[calc(4.5rem+env(safe-area-inset-top))] md:px-10 md:pt-12">
        {children}
      </div>
    </div>
  );
}
