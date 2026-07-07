"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import {
  FolderOpen,
  Home,
  LogOut,
  Menu,
  Search,
  Settings,
  UserCheck,
  Users,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { IndexStatusBanner } from "@/components/index-status-banner";
import { ThemeToggle } from "@/components/theme-toggle";
import { getAuthEmail, signOut } from "@/components/auth-gate";

const links = [
  { href: "/", label: "Dashboard", icon: Home, mobile: true },
  { href: "/review", label: "Review Queue", icon: UserCheck, mobile: true },
  { href: "/people", label: "People", icon: Users, mobile: true },
  { href: "/search", label: "Search", icon: Search, mobile: true },
  { href: "/folders", label: "Folders", icon: FolderOpen, mobile: true },
  { href: "/settings", label: "Settings", icon: Settings, mobile: false },
];

function BrandMark({ compact = false }: { compact?: boolean }) {
  return (
    <div className={cn("flex items-center gap-2", compact ? "min-w-0" : "")}>
      <div className="inline-flex shrink-0 items-center rounded-xl bg-gradient-to-br from-amber-400 to-orange-500 px-2.5 py-1 shadow-sm">
        <span className="text-sm font-bold tracking-tight text-white">DFI</span>
      </div>
      {!compact && (
        <div className="min-w-0">
          <h1 className="truncate text-base font-semibold tracking-tight text-sidebar-foreground">
            DriveFaceIndexer
          </h1>
          <p className="text-xs text-muted-foreground">Gemini video · Gemini images</p>
        </div>
      )}
    </div>
  );
}

function NavLinks({
  pathname,
  onNavigate,
  vertical = true,
  mobileOnly = false,
}: {
  pathname: string;
  onNavigate?: () => void;
  vertical?: boolean;
  mobileOnly?: boolean;
}) {
  const items = mobileOnly ? links.filter((l) => l.mobile) : links;

  return (
    <nav className={cn(vertical ? "space-y-0.5" : "flex items-center justify-around gap-1")}>
      {items.map(({ href, label, icon: Icon }) => (
        <Link
          key={href}
          href={href}
          onClick={onNavigate}
          className={cn(
            vertical
              ? "flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-all duration-150"
              : "flex min-h-11 min-w-0 flex-1 flex-col items-center justify-center gap-1 rounded-lg px-1 py-2 text-[10px] font-medium transition-colors",
            pathname === href
              ? vertical
                ? "bg-sidebar-primary text-sidebar-primary-foreground shadow-sm"
                : "text-primary"
              : vertical
                ? "text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
                : "text-muted-foreground hover:text-foreground"
          )}
        >
          <Icon size={vertical ? 16 : 18} className="shrink-0" />
          <span className={cn(!vertical && "truncate")}>{vertical ? label : label.split(" ")[0]}</span>
        </Link>
      ))}
    </nav>
  );
}

function SidebarFooter({ email }: { email: string | null }) {
  return (
    <div className="space-y-2 border-t border-sidebar-border pt-4">
      {email && (
        <div className="flex items-center justify-between gap-2">
          <p className="truncate text-[10px] text-muted-foreground">{email}</p>
          <button
            onClick={signOut}
            title="Sign out"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground"
          >
            <LogOut size={13} />
          </button>
        </div>
      )}
      <div className="flex items-center justify-between">
        <p className="text-[10px] text-muted-foreground">☀️ Summer Edition</p>
        <ThemeToggle />
      </div>
    </div>
  );
}

export function Sidebar() {
  const pathname = usePathname();
  const [email, setEmail] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    setEmail(getAuthEmail());
  }, []);

  useEffect(() => {
    setMenuOpen(false);
  }, [pathname]);

  useEffect(() => {
    document.body.style.overflow = menuOpen ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [menuOpen]);

  const currentPage = links.find((l) => l.href === pathname)?.label ?? "DriveFaceIndexer";

  return (
    <>
      {/* Desktop sidebar — unchanged layout at md+ */}
      <aside className="hidden w-56 shrink-0 flex-col border-r border-sidebar-border bg-sidebar p-4 md:flex">
        <div className="mb-8">
          <BrandMark />
        </div>
        <IndexStatusBanner />
        <NavLinks pathname={pathname} />
        <div className="mt-auto">
          <SidebarFooter email={email} />
        </div>
      </aside>

      {/* Mobile top bar */}
      <header className="fixed inset-x-0 top-0 z-40 flex h-14 items-center justify-between border-b border-border bg-background/95 px-4 backdrop-blur md:hidden">
        <BrandMark compact />
        <div className="flex min-w-0 items-center gap-2">
          <p className="truncate text-xs text-muted-foreground">{currentPage}</p>
          <button
            type="button"
            onClick={() => setMenuOpen(true)}
            aria-label="Open menu"
            className="flex h-10 w-10 items-center justify-center rounded-lg border border-border text-foreground"
          >
            <Menu size={18} />
          </button>
        </div>
      </header>

      {/* Mobile drawer */}
      {menuOpen && (
        <div className="fixed inset-0 z-50 md:hidden">
          <button
            type="button"
            aria-label="Close menu"
            className="absolute inset-0 bg-black/50"
            onClick={() => setMenuOpen(false)}
          />
          <aside className="absolute inset-y-0 right-0 flex w-[min(20rem,88vw)] flex-col border-l border-sidebar-border bg-sidebar p-4 shadow-xl">
            <div className="mb-4 flex items-center justify-between gap-2">
              <BrandMark compact />
              <button
                type="button"
                onClick={() => setMenuOpen(false)}
                aria-label="Close menu"
                className="flex h-10 w-10 items-center justify-center rounded-lg text-muted-foreground hover:bg-sidebar-accent"
              >
                <X size={18} />
              </button>
            </div>
            <IndexStatusBanner />
            <NavLinks pathname={pathname} onNavigate={() => setMenuOpen(false)} />
            <div className="mt-auto">
              <SidebarFooter email={email} />
            </div>
          </aside>
        </div>
      )}

      {/* Mobile bottom nav — quick access to main routes */}
      <nav className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-background/95 px-2 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden">
        <NavLinks pathname={pathname} vertical={false} mobileOnly />
      </nav>
    </>
  );
}
