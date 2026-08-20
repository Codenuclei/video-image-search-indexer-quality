import type { Metadata } from "next";
import { Inter } from "next/font/google";
import { Toaster } from "@/components/sonner";
import "./globals.css";
import { AppChrome } from "@/components/app-chrome";
import { AuthGate } from "@/components/auth-gate";
import { CacheSyncBoot } from "@/components/cache-sync-boot";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "DriveFaceIndexer",
  description: "Face recognition index for Google Drive",
  icons: {
    icon: [
      { url: "/favicon.ico" },
      { url: "/favicon-16x16.png", sizes: "16x16", type: "image/png" },
      { url: "/favicon-32x32.png", sizes: "32x32", type: "image/png" },
      { url: "/favicon-96x96.png", sizes: "96x96", type: "image/png" },
    ],
    apple: "/apple-touch-icon.png",
  },
};

export const viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `try{var t=localStorage.getItem("theme");var d=t==="dark"||(!t&&matchMedia("(prefers-color-scheme: dark)").matches);if(d)document.documentElement.classList.add("dark")}catch(e){}`,
          }}
        />
      </head>
      <body className={inter.className}>
        <CacheSyncBoot />
        <AuthGate>
          <AppChrome>{children}</AppChrome>
        </AuthGate>
        <Toaster />
      </body>
    </html>
  );
}
