"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  FileText,
  FolderOpen,
  HardDrive,
  Image as ImageIcon,
  ImagePlus,
  LogOut,
  Search,
  Settings,
  Users,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { getAuthEmail, signOut } from "@/components/auth-gate";
import { apiClient, type DriveSession } from "@/lib/api";
import { ThemeToggle } from "@/components/theme-toggle";
import { DriveSessionBar } from "@/components/drive-session-bar";
import { Spinner } from "@/components/ui";
import {
  hydrateSearchCatalogs,
  hydrateSearchSettings,
  patchSearchSession,
  persistSearchCaptions,
  persistSearchRerank,
  runSearch,
  useSearchSession,
} from "@/lib/search-session";
import { runReverseFaceSearch, setReverseFaceFile } from "@/lib/reverse-face-session";

const libraryLinks = [
  { href: "/test/folders", label: "Indexed Folders", icon: FolderOpen },
  { href: "/test/people", label: "People Directory", icon: Users },
];

function IconSelect({
  icon: Icon,
  title,
  value,
  active,
  disabled,
  onChange,
  children,
}: {
  icon: LucideIcon;
  title: string;
  value: string;
  active: boolean;
  disabled?: boolean;
  onChange: (value: string) => void;
  children: React.ReactNode;
}) {
  return (
    <label
      title={title}
      className={cn(
        "relative flex h-8 w-8 shrink-0 items-center justify-center rounded-full transition-colors",
        active ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
        disabled && "opacity-40"
      )}
    >
      <Icon size={15} />
      <select
        aria-label={title}
        className="absolute inset-0 h-full w-full cursor-pointer appearance-none opacity-0"
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        {children}
      </select>
    </label>
  );
}

function IconToggle({
  icon: Icon,
  title,
  active,
  onClick,
}: {
  icon: LucideIcon;
  title: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      title={title}
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "flex h-8 w-8 shrink-0 items-center justify-center rounded-full transition-colors",
        active ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
      )}
    >
      <Icon size={15} />
    </button>
  );
}

export function TestShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const {
    q,
    mime,
    folderPath,
    rerank,
    useCaptions,
    folderContexts,
    loading,
  } = useSearchSession();
  const [authEmail, setAuthEmail] = useState<string | null>(null);
  const [driveSession, setDriveSession] = useState<DriveSession | null>(null);
  const [headerQuery, setHeaderQuery] = useState("");
  const [accountOpen, setAccountOpen] = useState(false);
  const [confirmAction, setConfirmAction] = useState<"signout" | "drive" | null>(null);
  const uploadRef = useRef<HTMLInputElement>(null);
  const accountRef = useRef<HTMLDivElement>(null);

  /** Profile shows Drive account email, not the app login email. */
  const driveEmail = driveSession?.email?.trim() || null;
  const profileEmail = driveEmail ?? authEmail;

  useEffect(() => {
    setAuthEmail(getAuthEmail());
    hydrateSearchCatalogs();
    hydrateSearchSettings();
    void apiClient
      .driveSession()
      .then((s) => setDriveSession(s))
      .catch(() => setDriveSession(null));
  }, []);

  useEffect(() => {
    if (!rerank) void persistSearchRerank(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rerank]);

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (!accountRef.current?.contains(e.target as Node)) {
        setAccountOpen(false);
        setConfirmAction(null);
      }
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, []);

  async function disconnectDrive() {
    try {
      await apiClient.driveLogout();
    } catch {
      /* toasted by api() */
    }
    window.location.reload();
  }

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

  function onUploadPicked(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setReverseFaceFile(file);
    if (pathname !== "/test/search") {
      router.push("/test/search#reverse-face");
    }
    void runReverseFaceSearch(file);
  }

  const searchActive = pathname === "/test/search" || pathname === "/test";
  const mimeValue = mime === "video" ? "all" : mime;

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
            <div className="flex h-11 items-center gap-1 rounded-full border border-border bg-muted/60 pl-3.5 pr-1.5 transition-colors focus-within:border-ring">
              <Search size={16} className="shrink-0 text-muted-foreground" />
              <input
                value={headerQuery}
                onChange={(e) => {
                  setHeaderQuery(e.target.value);
                  patchSearchSession({ q: e.target.value });
                }}
                placeholder="Search photos, folders, or metadata..."
                className="min-w-0 flex-1 bg-transparent px-2 text-sm text-foreground outline-none placeholder:text-muted-foreground"
              />
              <div className="hidden items-center gap-0.5 sm:flex">
                <IconSelect
                  icon={ImageIcon}
                  title={`Type: ${mimeValue}`}
                  value={mimeValue}
                  active={mimeValue !== "all"}
                  onChange={(v) => patchSearchSession({ mime: v })}
                >
                  <option value="all">All types</option>
                  <option value="image">Images</option>
                  <option value="pdf">PDFs</option>
                </IconSelect>
                <IconSelect
                  icon={FolderOpen}
                  title={folderPath ? `Folder: ${folderPath}` : "All folders"}
                  value={folderPath}
                  active={folderPath !== ""}
                  onChange={(v) => patchSearchSession({ folderPath: v })}
                >
                  <option value="">All folders</option>
                  {folderContexts.map((f) => (
                    <option key={f.folder_path} value={f.folder_path} title={f.description}>
                      {f.folder_path.split("/").filter(Boolean).pop() ?? f.folder_path}
                    </option>
                  ))}
                </IconSelect>
                <IconToggle
                  icon={FileText}
                  title={useCaptions ? "Captions on" : "Captions off"}
                  active={useCaptions}
                  onClick={() => void persistSearchCaptions(!useCaptions)}
                />
              </div>
              <button
                type="button"
                title="Search by image"
                onClick={() => uploadRef.current?.click()}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
              >
                <ImagePlus size={15} />
              </button>
              <input
                ref={uploadRef}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={onUploadPicked}
              />
              <button
                type="submit"
                title="Search"
                disabled={loading}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-blue-600 text-white shadow-sm transition-colors hover:bg-blue-500 disabled:opacity-60"
              >
                {loading ? <Spinner size={14} /> : <Search size={14} />}
              </button>
            </div>
          </form>
          <DriveSessionBar compact />
          <ThemeToggle />
          <div ref={accountRef} className="relative">
            <button
              type="button"
              onClick={() => setAccountOpen((v) => !v)}
              className="flex h-10 w-10 items-center justify-center rounded-full bg-muted text-xs font-semibold text-foreground transition-colors hover:bg-accent"
              title={profileEmail ?? "Account"}
            >
              {(profileEmail ?? "U").slice(0, 1).toUpperCase()}
            </button>
            {accountOpen && (
              <div className="absolute right-0 top-12 z-30 w-64 overflow-hidden rounded-xl border border-border bg-card shadow-lg">
                <div className="border-b border-border px-4 py-3">
                  <p className="truncate text-xs font-medium text-foreground" title={driveEmail ?? undefined}>
                    {driveEmail ?? "Drive not connected"}
                  </p>
                  {driveSession?.selected_folder?.name && (
                    <p
                      className="mt-0.5 truncate text-[10px] text-muted-foreground"
                      title={driveSession.selected_folder.name}
                    >
                      {driveSession.selected_folder.name}
                    </p>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => {
                    if (confirmAction === "drive") {
                      void disconnectDrive();
                    } else {
                      setConfirmAction("drive");
                    }
                  }}
                  className={cn(
                    "flex w-full items-center gap-2 px-4 py-2.5 text-sm transition-colors",
                    confirmAction === "drive"
                      ? "bg-red-500/10 font-medium text-red-600 dark:text-red-400"
                      : "text-foreground hover:bg-accent"
                  )}
                >
                  <HardDrive size={14} />
                  {confirmAction === "drive" ? "Confirm disconnect Drive?" : "Disconnect Drive"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    if (confirmAction === "signout") {
                      signOut();
                    } else {
                      setConfirmAction("signout");
                    }
                  }}
                  className={cn(
                    "flex w-full items-center gap-2 px-4 py-2.5 text-sm transition-colors",
                    confirmAction === "signout"
                      ? "bg-red-500/10 font-medium text-red-600 dark:text-red-400"
                      : "text-foreground hover:bg-accent"
                  )}
                >
                  <LogOut size={14} />
                  {confirmAction === "signout" ? "Confirm sign out?" : "Sign out"}
                </button>
              </div>
            )}
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
