"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Loader2, Search, Sparkles, Wand2 } from "lucide-react";
import {
  type CarouselLlmModelOption,
  type CarouselLlmProvider,
  type CarouselRunConfig,
} from "@/lib/test-api";
import { cn } from "@/lib/utils";
import { useCarouselLlmCatalog } from "./llm-catalog";
import { ProviderBrandIcon } from "./provider-icons";

function providerLabel(
  providers: { id: string; label: string }[],
  provider: string
): string {
  return providers.find((p) => p.id === provider)?.label || provider;
}

function modelOptionKey(option: Pick<CarouselLlmModelOption, "provider" | "id">): string {
  return `${option.provider}:${option.id}`;
}

export { useCarouselLlmCatalog } from "./llm-catalog";

export function StageLlmGenerate({
  label,
  busy = false,
  disabled = false,
  runConfig,
  onRunConfigChange,
  onGenerate,
  className,
  testId,
}: {
  label: string;
  busy?: boolean;
  disabled?: boolean;
  runConfig: CarouselRunConfig;
  onRunConfigChange?: (next: CarouselRunConfig) => void;
  onGenerate: (cfg: CarouselRunConfig) => void | Promise<void>;
  className?: string;
  testId?: string;
}) {
  const { models, providers } = useCarouselLlmCatalog();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    requestAnimationFrame(() => searchRef.current?.focus());
    const onDoc = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const grouped = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const order = providers.map((p) => p.id).filter((id) => id !== "auto");
    const byProvider = new Map<string, CarouselLlmModelOption[]>();
    for (const model of models) {
      if (
        needle &&
        !model.label.toLowerCase().includes(needle) &&
        !model.id.toLowerCase().includes(needle) &&
        !model.provider.toLowerCase().includes(needle)
      ) {
        continue;
      }
      const list = byProvider.get(model.provider) ?? [];
      list.push(model);
      byProvider.set(model.provider, list);
    }
    const groups = order
      .filter((id) => byProvider.has(id))
      .map((id) => ({
        id,
        label: providerLabel(providers, id),
        models: byProvider.get(id) ?? [],
      }));
    for (const [id, list] of byProvider) {
      if (order.includes(id)) continue;
      groups.push({ id, label: providerLabel(providers, id), models: list });
    }
    return groups;
  }, [models, providers, query]);

  const currentLabel = useMemo(() => {
    const hit = models.find(
      (m) => m.provider === runConfig.provider && m.id === runConfig.model
    );
    if (hit) return hit.label;
    return runConfig.model;
  }, [models, runConfig.model, runConfig.provider]);

  async function runWith(cfg: CarouselRunConfig) {
    onRunConfigChange?.(cfg);
    setOpen(false);
    await onGenerate(cfg);
  }

  return (
    <div
      ref={rootRef}
      className={cn("stage-llm-gen", open && "is-open", className)}
      data-testid={testId || "stage-llm-generate"}
    >
      <div className="stage-llm-gen-split" role="group" aria-label={label}>
        <button
          type="button"
          className="stage-llm-gen-main"
          disabled={disabled || busy}
          onClick={() => void runWith(runConfig)}
          title={`${label} · ${currentLabel}`}
        >
          {busy ? (
            <Loader2 size={14} className="animate-spin stage-llm-gen-brand" aria-hidden />
          ) : (
            <ProviderBrandIcon
              provider={runConfig.provider}
              modelId={runConfig.model}
              className="llm-brand-icon stage-llm-gen-brand"
            />
          )}
          <span className="stage-llm-gen-main-copy">
            <span className="stage-llm-gen-main-label-row">
              <span className="stage-llm-gen-main-label">
                {busy ? "Generating…" : label}
              </span>
              {!busy ? <Wand2 size={14} className="stage-llm-gen-wand" aria-hidden /> : null}
            </span>
            {!busy ? (
              <span className="stage-llm-gen-main-model">{currentLabel}</span>
            ) : null}
          </span>
        </button>
        <button
          type="button"
          className="stage-llm-gen-chevron"
          disabled={disabled || busy}
          aria-expanded={open}
          aria-haspopup="menu"
          onClick={() => setOpen((v) => !v)}
          aria-label={`${label} — choose model`}
        >
          <ChevronDown size={14} aria-hidden />
        </button>
      </div>

      {open ? (
        <div className="stage-llm-gen-menu" role="menu" aria-label={`${label} models`}>
          <div className="stage-llm-gen-menu-head">
            <Sparkles size={13} aria-hidden />
            <span>Choose model</span>
          </div>
          <div className="stage-llm-gen-search">
            <Search size={14} className="stage-llm-gen-search-icon" aria-hidden />
            <input
              ref={searchRef}
              className="stage-llm-gen-search-input"
              type="search"
              placeholder="Search models…"
              value={query}
              autoComplete="off"
              onChange={(e) => setQuery(e.target.value)}
              aria-label="Search models"
            />
          </div>
          <div className="stage-llm-gen-menu-scroll">
            {grouped.map((group) => (
              <div key={group.id} className="stage-llm-gen-group">
                <p className="stage-llm-gen-group-label">{group.label}</p>
                <ul className="stage-llm-gen-list">
                  {group.models.map((option, idx) => {
                    const active =
                      runConfig.provider === option.provider && runConfig.model === option.id;
                    return (
                      <li key={`${modelOptionKey(option)}:${option.label}:${idx}`}>
                        <button
                          type="button"
                          role="menuitem"
                          className={cn("stage-llm-gen-option", active && "is-active")}
                          onClick={() =>
                            void runWith({
                              provider: option.provider as CarouselLlmProvider,
                              model: option.id,
                            })
                          }
                        >
                          <span className="stage-llm-gen-option-check" aria-hidden>
                            {active ? <Check size={12} strokeWidth={2.5} /> : null}
                          </span>
                          <ProviderBrandIcon
                            provider={option.provider}
                            modelId={option.id}
                            className="llm-brand-icon"
                          />
                          <span className="stage-llm-gen-option-label">{option.label}</span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))}
            {grouped.length === 0 ? (
              <p className="stage-llm-gen-empty">No models match</p>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
