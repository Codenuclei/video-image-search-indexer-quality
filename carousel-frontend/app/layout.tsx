import type { Metadata } from "next";
import SmoothScroll from "@/components/smooth-scroll";
import "./globals.css";

export const metadata: Metadata = {
  title: "Carousel Studio",
  description:
    "Build Instagram-ready carousels from indexed videos — themes, hooks, and frames in one studio.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="antialiased">
      <head>
        <link
          rel="stylesheet"
          href="https://db.onlinewebfonts.com/c/9d4d074c9335825a23cce178ee03b498?family=P22+Mackinac+W01+Book"
        />
      </head>
      {/* System stack avoids next/font/google build-time fetches (flaky on Railway). */}
      <body className="min-h-full font-sans antialiased bg-white text-[#191919]">
        <SmoothScroll>{children}</SmoothScroll>
      </body>
    </html>
  );
}
