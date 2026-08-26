"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  CircleHelp,
  FileText,
  FolderOpen,
  HardDrive,
  Image as ImageIcon,
  LogOut,
  ScanFace,
  Search,
  Settings,
  Users,
  X,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { FilterDropdown } from "@/components/filter-dropdown";
import { getAuthEmail, signOut } from "@/components/auth-gate";
import { apiClient, type DriveSession } from "@/lib/api";
import { ThemeToggle } from "@/components/theme-toggle";
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
import {
  clearReverseFaceSearch,
  runReverseFaceSearch,
  setReverseFaceFile,
  useReverseFaceSession,
} from "@/lib/reverse-face-session";
import { useTestShellChrome } from "@/lib/test-shell-chrome";

const libraryLinks = [
  { href: "/test/folders", label: "Indexed Folders", icon: FolderOpen },
  { href: "/test/people", label: "People Directory", icon: Users },
];

function IconToggle({
  icon: Icon,
  title,
  active,
  activeClassName,
  onClick,
}: {
  icon: LucideIcon;
  title: string;
  active: boolean;
  activeClassName?: string;
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
        active
          ? activeClassName ?? "bg-primary/10 text-primary"
          : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
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
    person,
    folderPath,
    rerank,
    useCaptions,
    folderContexts,
    libraryFolders,
    persons,
    loading,
  } = useSearchSession();
  const [authEmail, setAuthEmail] = useState<string | null>(null);
  const [driveSession, setDriveSession] = useState<DriveSession | null>(null);
  const [headerQuery, setHeaderQuery] = useState("");
  const [accountOpen, setAccountOpen] = useState(false);
  const [confirmAction, setConfirmAction] = useState<"signout" | "drive" | null>(null);
  const uploadRef = useRef<HTMLInputElement>(null);
  const { file: reverseFaceFile, result: reverseFaceResult } = useReverseFaceSession();
  const pageChrome = useTestShellChrome();
  const accountRef = useRef<HTMLDivElement>(null);

  const driveEmail = driveSession?.email?.trim() || null;
  const profileEmail = driveEmail ?? authEmail;
  const searchActive = pathname === "/test/search" || pathname === "/test";
  const compactHeader = !searchActive;
  const mimeValue = mime === "video" ? "all" : mime;

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

  useEffect(() => {
    if (searchActive) setHeaderQuery(q);
  }, [searchActive, q]);

  async function disconnectDrive() {
    try {
      await apiClient.driveLogout();
    } catch {
      /* toasted by api() */
    }
    window.location.reload();
  }

  function submitHeaderSearch(event: React.FormEvent) {
    event.preventDefault();
    patchSearchSession({ q: headerQuery });
    void runSearch();
  }

  function onUploadPicked(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setReverseFaceFile(file);
    router.push("/test/search#reverse-face");
    void runReverseFaceSearch(file);
  }

  return (
    <div className="flex h-dvh overflow-hidden bg-muted/40 text-foreground">
      <aside className="hidden h-full w-[13.5rem] shrink-0 flex-col border-r border-sidebar-border bg-sidebar md:flex">
        <div className="px-3 pb-2 pt-5">
          <Link href="/test/search" className="block">
            <p className="text-lg font-semibold tracking-tight text-blue-700 dark:text-blue-400">DriveFaceIndexer</p>
          </Link>
        </div>

        <nav className="scrollbar-hidden min-h-0 flex-1 space-y-5 overflow-y-auto px-2 pb-4 pt-2">
          <div>
            <Link
              href="/test/search"
              className={cn(
                "flex items-center gap-2 rounded-xl px-2.5 py-2 text-sm font-medium transition-colors",
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
            <p className="mb-1.5 flex items-center gap-2 border-b border-sidebar-border px-2.5 pb-2 text-xs font-bold uppercase tracking-[0.14em] text-foreground">
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
                      "flex items-center gap-2 rounded-xl px-2.5 py-2 text-sm font-medium transition-colors",
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

        <div className="space-y-1 border-t border-sidebar-border p-2">
          <Link
            href="/test/help"
            className={cn(
              "flex items-center gap-2 rounded-xl px-2.5 py-2 text-sm transition-colors",
              pathname.startsWith("/test/help")
                ? "bg-card text-blue-700 shadow-sm dark:text-blue-300"
                : "text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
            )}
          >
            <CircleHelp size={16} />
            How to / FAQ
          </Link>
          <Link
            href="/settings"
            className="flex items-center gap-2 rounded-xl px-2.5 py-2 text-sm text-sidebar-foreground/80 transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
          >
            <Settings size={16} />
            Settings
          </Link>
          <Link
            href="/"
            className="flex items-center gap-2 rounded-xl px-2.5 py-2 text-sm text-sidebar-foreground/80 transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
          >
            <HardDrive size={16} />
            Current UI
          </Link>
        </div>
      </aside>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <header
          className={cn(
            "z-20 flex min-h-[3.25rem] shrink-0 items-center gap-3 px-4 md:px-6",
            compactHeader
              ? "justify-between border-b border-border bg-card py-2.5"
              : "border-b border-border bg-card py-3"
          )}
        >
          {searchActive ? (
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
                  <FilterDropdown
                    iconOnly
                    icon={ImageIcon}
                    title={`Type: ${mimeValue}`}
                    value={mimeValue}
                    active={mimeValue !== "all"}
                    onChange={(v) => patchSearchSession({ mime: v })}
                    options={[
                      { value: "all", label: "All types" },
                      { value: "image", label: "Images" },
                      { value: "pdf", label: "PDFs" },
                    ]}
                  />
                  <FilterDropdown
                    iconOnly
                    icon={FolderOpen}
                    title={
                      folderPath
                        ? libraryFolders.find((f) => f.value === folderPath)?.label ??
                          folderContexts.find((f) => f.folder_path === folderPath)?.description ??
                          `Folder: ${folderPath}`
                        : "All folders"
                    }
                    value={folderPath}
                    active={folderPath !== ""}
                    onChange={(v) => patchSearchSession({ folderPath: v })}
                    options={[
                      { value: "", label: "All folders" },
                      ...libraryFolders.map((f) => ({
                        value: f.value,
                        label: f.label,
                        hint: folderContexts.find((c) => c.folder_path === f.value)?.description,
                      })),
                    ]}
                  />
                  <FilterDropdown
                    iconOnly
                    icon={Users}
                    title={person ? `Person: ${person}` : "All people"}
                    value={person}
                    active={person !== ""}
                    disabled={persons.length === 0}
                    onChange={(v) => patchSearchSession({ person: v })}
                    options={[
                      { value: "", label: "All people" },
                      ...persons.map((p) => ({
                        value: p.name,
                        label: p.name,
                        faceId: p.representative_face_id,
                      })),
                    ]}
                  />
                  <IconToggle
                    icon={FileText}
                    title={useCaptions ? "Captions on" : "Captions off"}
                    active={useCaptions}
                    activeClassName="bg-blue-500/10 text-blue-600 dark:bg-blue-400/15 dark:text-blue-300"
                    onClick={() => void persistSearchCaptions(!useCaptions)}
                  />
                </div>
                <button
                  type="button"
                  title="Search by image"
                  onClick={() => uploadRef.current?.click()}
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
                >
                  <ScanFace size={15} />
                </button>
                {(reverseFaceFile || reverseFaceResult) && (
                  <button
                    type="button"
                    title="Clear image search"
                    aria-label="Clear image search"
                    onClick={() => clearReverseFaceSearch()}
                    className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
                  >
                    <X size={15} />
                  </button>
                )}
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
          ) : (
            <div className="min-w-0 flex-1 overflow-hidden">{pageChrome}</div>
          )}
          <div className="flex shrink-0 items-center gap-2">
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
          </div>
        </header>

        <nav
          className={cn(
            "flex gap-2 overflow-x-auto px-4 py-2 md:hidden",
            compactHeader ? "border-0 bg-transparent" : "border-b border-border bg-card"
          )}
        >
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

        <main
          className={cn(
            "scrollbar-hidden min-h-0 flex-1 overflow-x-hidden overflow-y-auto px-4 md:px-8",
            compactHeader ? "pb-6 pt-2" : "py-6"
          )}
        >
          {children}
        </main>
      </div>
    </div>
  );
}
