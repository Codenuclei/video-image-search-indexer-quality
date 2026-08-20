"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  FolderOpen,
  HardDrive,
  Search,
  Settings,
  Upload,
  Users,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { getAuthEmail } from "@/components/auth-gate";
import { ThemeToggle } from "@/components/theme-toggle";
import { DriveSessionBar } from "@/components/drive-session-bar";
import { Input } from "@/components/ui";
import {
  hydrateSearchCatalogs,
  hydrateSearchSettings,
  patchSearchSession,
  runSearch,
  useSearchSession,
} from "@/lib/search-session";

const libraryLinks = [
  { href: "/test/folders", label: "Indexed Folders", icon: FolderOpen },
  { href: "/test/people", label: "People Directory", icon: Users },
];

export function TestShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { q } = useSearchSession();
  const [email, setEmail] = useState<string | null>(null);
  const [headerQuery, setHeaderQuery] = useState("");

  useEffect(() => {
    setEmail(getAuthEmail());
    hydrateSearchCatalogs();
    hydrateSearchSettings();
  }, []);

  useEffect(() => {
    if (pathname === "/test/search" || pathname === "/test") {
      setHeaderQuery(q);
    }
  }, [pathname, q]);

  function submitHeaderSearch(event: React.FormEvent) {
    event.preventDefault();
    patchSearchSession({ q: headerQuery });
    if (pathname !== "/test/search") {
      router.push("/test/search");
    }
    void runSearch();
  }

  const searchActive = pathname === "/test/search" || pathname === "/test";

  return (
    <div className="flex min-h-screen bg-muted/40 text-foreground">
      <aside className="sticky top-0 hidden h-screen w-[16.5rem] shrink-0 flex-col border-r border-sidebar-border bg-sidebar md:flex">
        <div className="px-5 pb-2 pt-6">
          <Link href="/test/search" className="block">
            <p className="text-lg font-semibold tracking-tight text-blue-700 dark:text-blue-400">DriveFaceIndexer</p>
          </Link>
        </div>

        <nav className="min-h-0 flex-1 space-y-5 overflow-y-auto px-3 pb-4 pt-2">
          <div>
            <Link
              href="/test/search"
              className={cn(
                "flex items-center gap-2.5 rounded-xl px-3 py-2 text-sm font-medium transition-colors",
                searchActive
                  ? "bg-card text-blue-700 shadow-sm dark:text-blue-300"
                  : "text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
              )}
            >
              <Search size={16} />
              Search
            </Link>
          </div>

          <div>
            <p className="mb-1.5 flex items-center gap-2 border-b border-sidebar-border px-3 pb-2 text-xs font-bold uppercase tracking-[0.18em] text-foreground">
              Library
            </p>
            <div className="space-y-0.5">
              {libraryLinks.map((item) => {
                const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
                const Icon = item.icon;
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={cn(
                      "flex items-center gap-2.5 rounded-xl px-3 py-2 text-sm font-medium transition-colors",
                      active
                        ? "bg-card text-blue-700 shadow-sm dark:text-blue-300"
                        : "text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
                    )}
                  >
                    <Icon size={16} />
                    {item.label}
                  </Link>
                );
              })}
            </div>
          </div>
        </nav>

        <div className="space-y-1 border-t border-sidebar-border p-3">
          <Link
            href="/settings"
            className="flex items-center gap-2.5 rounded-xl px-3 py-2 text-sm text-sidebar-foreground/80 transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
          >
            <Settings size={16} />
            Settings
          </Link>
          <Link
            href="/"
            className="flex items-center gap-2.5 rounded-xl px-3 py-2 text-sm text-sidebar-foreground/80 transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
          >
            <HardDrive size={16} />
            Current UI
          </Link>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex items-center gap-3 border-b border-border bg-card/90 px-4 py-3 backdrop-blur md:px-6">
          <form onSubmit={submitHeaderSearch} className="min-w-0 flex-1">
            <div className="relative">
              <Search size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={headerQuery}
                onChange={(e) => {
                  setHeaderQuery(e.target.value);
                  patchSearchSession({ q: e.target.value });
                }}
                placeholder="Search photos, folders, or metadata..."
                className="h-11 rounded-full border-border bg-muted/60 pl-9"
              />
            </div>
          </form>
          <DriveSessionBar compact />
          <Link
            href="/test/search#reverse-face"
            className="hidden h-11 w-11 items-center justify-center rounded-full border border-border text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground md:inline-flex"
            title="Reverse face"
          >
            <Upload size={16} />
          </Link>
          <ThemeToggle />
          <div
            className="flex h-10 w-10 items-center justify-center rounded-full bg-muted text-xs font-semibold text-foreground"
            title={email ?? "Signed in"}
          >
            {(email ?? "U").slice(0, 1).toUpperCase()}
          </div>
        </header>

        <nav className="flex gap-2 overflow-x-auto border-b border-border bg-card px-4 py-2 md:hidden">
          <Link
            href="/test/search"
            className={cn(
              "rounded-full px-3 py-1.5 text-xs font-medium transition-colors",
              searchActive ? "bg-blue-600 text-white" : "bg-muted text-foreground"
            )}
          >
            Search
          </Link>
          <Link
            href="/test/folders"
            className={cn(
              "rounded-full px-3 py-1.5 text-xs font-medium transition-colors",
              pathname.startsWith("/test/folders") ? "bg-blue-600 text-white" : "bg-muted text-foreground"
            )}
          >
            Folders
          </Link>
          <Link
            href="/test/people"
            className={cn(
              "rounded-full px-3 py-1.5 text-xs font-medium transition-colors",
              pathname.startsWith("/test/people") ? "bg-blue-600 text-white" : "bg-muted text-foreground"
            )}
          >
            People
          </Link>
        </nav>

        <main className="flex-1 overflow-y-auto px-4 py-6 md:px-8">{children}</main>
      </div>
    </div>
  );
}
