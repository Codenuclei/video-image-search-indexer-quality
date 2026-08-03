import type { ReactNode } from "react";

/** Soft-disabled route — no studio chrome; page redirects to /search. */
export default function CarouselStudioLayout({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
