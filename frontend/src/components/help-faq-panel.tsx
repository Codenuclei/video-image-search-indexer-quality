"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ChevronDown, CircleHelp, ExternalLink } from "lucide-react";
import { FAQ_CATEGORIES, type FaqCategory } from "@/lib/help-faq";
import { Card } from "@/components/ui";
import { cn } from "@/lib/utils";

export type HelpFaqAccent = "amber" | "blue";

const ACCENT = {
  amber: {
    icon: "text-amber-600 dark:text-amber-400",
    pillActive: "border-amber-500/50 bg-amber-500/10 text-foreground",
  },
  blue: {
    icon: "text-blue-600 dark:text-blue-400",
    pillActive: "border-blue-500/50 bg-blue-500/10 text-foreground",
  },
} as const;

function defaultLinkLabel(href: string): string {
  if (href === "/") return "Dashboard";
  if (href === "/test/search" || href.startsWith("/test/search#")) return "Search";
  if (href === "/test/folders") return "Indexed Folders";
  if (href.startsWith("/test/people")) return "People Directory";
  if (href === "/test/help") return "How to / FAQ";
  if (href === "/settings") return "Settings";
  if (href.includes("reverse-face")) return "Search by image";
  return href;
}

export function HelpFaqPanel({
  accent = "amber",
  categories = FAQ_CATEGORIES,
  mapHref,
  showTitle = true,
}: {
  accent?: HelpFaqAccent;
  categories?: FaqCategory[];
  mapHref?: (href: string) => string;
  /** When false, omit the in-page h2 (use TestShell header instead). */
  showTitle?: boolean;
}) {
  const styles = ACCENT[accent];
  const allIds = useMemo(
    () => categories.flatMap((c) => c.items.map((i) => i.id)),
    [categories]
  );
  const [openId, setOpenId] = useState<string | null>(allIds[0] ?? null);
  const [activeCategory, setActiveCategory] = useState(categories[0]?.id ?? "");

  useEffect(() => {
    setOpenId(allIds[0] ?? null);
    setActiveCategory(categories[0]?.id ?? "");
  }, [allIds, categories]);

  useEffect(() => {
    const hash = typeof window !== "undefined" ? window.location.hash.replace(/^#/, "") : "";
    if (!hash) return;
    const match = categories
      .flatMap((c) => c.items.map((item) => ({ categoryId: c.id, itemId: item.id })))
      .find((x) => x.itemId === hash || x.categoryId === hash);
    if (match) {
      setActiveCategory(match.categoryId);
      if (match.itemId === hash) setOpenId(hash);
      requestAnimationFrame(() => {
        document.getElementById(hash)?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
  }, [categories]);

  function toggle(id: string) {
    setOpenId((prev) => (prev === id ? null : id));
  }

  function resolveHref(href: string): string {
    return mapHref ? mapHref(href) : href;
  }

  return (
    <div className="mx-auto w-full max-w-5xl space-y-6">
      {showTitle && (
        <div>
          <h2 className="flex items-center gap-2 text-2xl font-semibold text-foreground">
            <CircleHelp size={22} className={styles.icon} aria-hidden />
            How to / FAQ
          </h2>
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {categories.map((cat) => (
          <a
            key={cat.id}
            href={`#${cat.id}`}
            onClick={(e) => {
              e.preventDefault();
              setActiveCategory(cat.id);
              document.getElementById(cat.id)?.scrollIntoView({ behavior: "smooth", block: "start" });
              window.history.replaceState(null, "", `#${cat.id}`);
            }}
            className={cn(
              "rounded-full border px-3 py-1.5 text-xs font-medium transition-colors",
              activeCategory === cat.id
                ? styles.pillActive
                : "border-border bg-card text-muted-foreground hover:bg-muted hover:text-foreground"
            )}
          >
            {cat.title}
          </a>
        ))}
      </div>

      <div className="space-y-8">
        {categories.map((cat) => (
          <section key={cat.id} id={cat.id} className="scroll-mt-20 space-y-3">
            <div>
              <h3 className="text-lg font-semibold text-foreground">{cat.title}</h3>
              <p className="text-sm text-muted-foreground">{cat.blurb}</p>
            </div>
            <div className="space-y-2">
              {cat.items.map((item) => {
                const open = openId === item.id;
                const href = item.href ? resolveHref(item.href) : undefined;
                return (
                  <div key={item.id} id={item.id} className="scroll-mt-24">
                    <Card className="overflow-hidden p-0">
                      <button
                        type="button"
                        onClick={() => {
                          toggle(item.id);
                          setActiveCategory(cat.id);
                          window.history.replaceState(null, "", `#${item.id}`);
                        }}
                        aria-expanded={open}
                        aria-controls={`${item.id}-panel`}
                        className="flex w-full items-start gap-3 px-4 py-3.5 text-left transition-colors hover:bg-muted/40"
                      >
                        <span className="min-w-0 flex-1 text-sm font-medium text-foreground">
                          {item.question}
                        </span>
                        <ChevronDown
                          size={16}
                          className={cn(
                            "mt-0.5 shrink-0 text-muted-foreground transition-transform",
                            open && "rotate-180"
                          )}
                          aria-hidden
                        />
                      </button>
                      {open && (
                        <div
                          id={`${item.id}-panel`}
                          className="border-t border-border/60 px-4 py-3"
                        >
                          <p className="whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">
                            {item.answer}
                          </p>
                          {href && (
                            <Link
                              href={href}
                              className="mt-3 inline-flex items-center gap-1.5 text-xs font-medium text-sky-700 hover:underline dark:text-sky-300"
                            >
                              Open {defaultLinkLabel(href)}
                              <ExternalLink size={11} aria-hidden />
                            </Link>
                          )}
                        </div>
                      )}
                    </Card>
                  </div>
                );
              })}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
