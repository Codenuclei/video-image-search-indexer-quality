import type { Metadata } from "next";
import { Inter } from "next/font/google";
import { Toaster } from "sonner";
import "./globals.css";
import { Sidebar } from "@/components/sidebar";
import { AuthGate } from "@/components/auth-gate";

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
    <html lang="en">
      <body className={inter.className}>
        <AuthGate>
          <div className="flex min-h-screen flex-col md:h-screen md:flex-row md:overflow-hidden">
            <Sidebar />
            <main className="flex-1 overflow-x-hidden overflow-y-auto px-4 pb-[calc(4.5rem+env(safe-area-inset-bottom))] pt-[calc(3.5rem+env(safe-area-inset-top))] md:min-h-0 md:overflow-y-auto md:p-8">
              {children}
            </main>
          </div>
        </AuthGate>
        <Toaster
          position="bottom-center"
          theme="dark"
          richColors
          closeButton
          toastOptions={{
            classNames: {
              toast:
                "border border-zinc-700/80 bg-zinc-950/95 text-zinc-100 shadow-xl backdrop-blur-sm",
              description: "text-zinc-400 text-xs",
              title: "text-sm font-medium text-zinc-100",
            },
          }}
        />
      </body>
    </html>
  );
}
