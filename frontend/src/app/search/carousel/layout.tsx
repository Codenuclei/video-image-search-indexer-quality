import type { ReactNode } from "react";
import "./carousel-studio.css";

export default function CarouselStudioLayout({ children }: { children: ReactNode }) {
  return <div className="carousel-studio">{children}</div>;
}
