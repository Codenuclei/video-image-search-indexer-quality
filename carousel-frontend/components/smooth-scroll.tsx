"use client";

import { useEffect, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import Lenis from "lenis";
import "lenis/dist/lenis.css";

/**
 * Document-level Lenis smooth scroll on the landing page only.
 * Studio (`/carousel`) uses native document + overflow-y scroll so nested
 * lists (e.g. video pick) are not intercepted.
 */
export default function SmoothScroll({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const enableLenis = pathname === "/";

  useEffect(() => {
    if (!enableLenis) return;

    const lenis = new Lenis({
      autoRaf: true,
      anchors: true,
      allowNestedScroll: true,
      stopInertiaOnNavigate: true,
    });

    // Smoothly land on hash targets after route / hard navigation
    const hash = window.location.hash;
    if (hash) {
      requestAnimationFrame(() => {
        lenis.scrollTo(hash, { immediate: false });
      });
    }

    return () => {
      lenis.destroy();
    };
  }, [enableLenis]);

  return <>{children}</>;
}
