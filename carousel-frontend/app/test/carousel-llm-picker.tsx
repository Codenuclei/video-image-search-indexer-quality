"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Search, Sparkles } from "lucide-react";
import { type CarouselRunConfig } from "@/lib/test-api";
import { cn } from "@/lib/utils";
import {
  DEFAULT_CAROUSEL_RUN_CONFIG,
  defaultModelForProvider,
  useCarouselLlmCatalog,
} from "./llm-catalog";
import { ProviderBrandIcon } from "./provider-icons";

export { DEFAULT_CAROUSEL_RUN_CONFIG };

const RUN_CONFIG_STORAGE_KEY = "test-carousel-run-config";

export function loadRunConfig(): CarouselRunConfig {
  if (typeof window === "undefined") return DEFAULT_CAROUSEL_RUN_CONFIG;
  try {
    const raw = sessionStorage.getItem(RUN_CONFIG_STORAGE_KEY);
    if (!raw) return DEFAULT_CAROUSEL_RUN_CONFIG;
    const parsed = JSON.parse(raw) as Partial<CarouselRunConfig>;
    if (parsed?.provider && parsed?.model) {
      return { provider: parsed.provider, model: parsed.model };
    }
  } catch {
    /* keep default */
  }
  return DEFAULT_CAROUSEL_RUN_CONFIG;
}

export function persistRunConfig(next: CarouselRunConfig) {
  if (typeof window === "undefined") return;
  try {
    sessionStorage.setItem(RUN_CONFIG_STORAGE_KEY, JSON.stringify(next));
  } catch {
    /* ignore quota / private mode */
  }
}

type ModelOption = { id: string; label: string; provider?: string };

/** Searchable model menu — Vercel/Rauno/Linear light tokens + Simple Icons. */
function SearchableModelSelect({
  value,
  options,
  routeProvider,
  disabled,
  onChange,
}: {
  value: string;
  options: ModelOption[];
  routeProvider: string;
  disabled?: boolean;
  onChange: (modelId: string) => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);

  const selected = options.find((o) => o.id === value);
  const selectedLabel = selected?.label || value || "Select a model";

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return options;
    return options.filter(
      (o) =>
        o.label.toLowerCase().includes(needle) || o.id.toLowerCase().includes(needle)
    );
  }, [options, query]);

  useEffect(() => {
    if (!open) return;
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

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActiveIndex(0);
    requestAnimationFrame(() => inputRef.current?.focus());
  }, [open]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  function pick(id: string) {
    onChange(id);
    setOpen(false);
  }

  return (
    <div
      ref={rootRef}
      className={cn("llm-search-select", open && "is-open")}
      data-testid="carousel-llm-model-search"
    >
      <button
        type="button"
        className="llm-search-select__trigger"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="Carousel run LLM model"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="llm-search-select__trigger-main">
          <ProviderBrandIcon
            provider={selected?.provider || routeProvider}
            modelId={value}
            className="llm-brand-icon"
          />
          <span className="llm-search-select__value">{selectedLabel}</span>
        </span>
        <ChevronDown size={14} className="llm-search-select__chevron" aria-hidden />
      </button>

      {open ? (
        <div className="llm-search-select__panel" role="listbox" aria-label="LLM models">
          <div className="llm-search-select__search">
            <Search size={14} className="llm-search-select__search-icon" aria-hidden />
            <input
              ref={inputRef}
              className="llm-search-select__input"
              type="search"
              placeholder="Search models…"
              value={query}
              autoComplete="off"
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setActiveIndex((i) => Math.min(i + 1, Math.max(filtered.length - 1, 0)));
                } else if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setActiveIndex((i) => Math.max(i - 1, 0));
                } else if (e.key === "Enter" && filtered[activeIndex]) {
                  e.preventDefault();
                  pick(filtered[activeIndex].id);
                }
              }}
            />
          </div>
          <ul className="llm-search-select__list">
            {filtered.map((option, index) => {
              const isSelected = option.id === value;
              const active = index === activeIndex;
              const rowKey = `${option.provider || routeProvider}:${option.id}:${option.label}`;
              return (
                <li key={rowKey}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={isSelected}
                    className={cn(
                      "llm-search-select__option",
                      isSelected && "is-selected",
                      active && "is-active"
                    )}
                    onMouseEnter={() => setActiveIndex(index)}
                    onClick={() => pick(option.id)}
                  >
                    <span className="llm-search-select__check" aria-hidden>
                      {isSelected ? <Check size={12} strokeWidth={2.5} /> : null}
                    </span>
                    <ProviderBrandIcon
                      provider={option.provider || routeProvider}
                      modelId={option.id}
                      className="llm-brand-icon"
                    />
                    <span className="llm-search-select__option-label">
                      {option.label || option.id}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          {filtered.length === 0 ? (
            <p className="llm-search-select__empty">No models match</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function CarouselLlmPicker({
  value,
  onChange,
  disabled = false,
  compact = true,
  variant = "card",
  showHint = true,
  showGenerate = false,
  generateBusy = false,
  onGenerate,
  className,
}: {
  value: CarouselRunConfig;
  onChange: (next: CarouselRunConfig) => void;
  disabled?: boolean;
  compact?: boolean;
  variant?: "card" | "inline";
  showHint?: boolean;
  /** Optional dedicated Generate control under the select (prompt Phase 3). */
  showGenerate?: boolean;
  generateBusy?: boolean;
  onGenerate?: (cfg: CarouselRunConfig) => void | Promise<void>;
  className?: string;
}) {
  const { models, providers } = useCarouselLlmCatalog();

  const filtered = useMemo(() => {
    if (value.provider === "claude") {
      const direct = models.filter((o) => o.provider === "claude");
      return direct.length ? direct : models.filter((o) => o.id.startsWith("anthropic/"));
    }
    if (value.provider === "openrouter" || value.provider === "auto") {
      return models.filter((o) => o.provider === "openrouter");
    }
    const direct = models.filter((o) => o.provider === "gemini");
    return direct.length
      ? direct
      : models.filter((o) => o.provider === "gemini" || o.id.startsWith("google/"));
  }, [models, value.provider]);

  const hasCurrent = filtered.some((option) => option.id === value.model);
  const providerChoices = providers.length
    ? providers
    : [
        { id: "claude", label: "Claude (direct)" },
        { id: "openrouter", label: "OpenRouter" },
        { id: "gemini", label: "Gemini" },
        { id: "auto", label: "Auto" },
      ];

  const modelOptions = useMemo(() => {
    const base: ModelOption[] = filtered.map((o) => ({
      id: o.id,
      label: o.label || o.id,
      provider: o.provider,
    }));
    if (!hasCurrent && value.model) {
      return [{ id: value.model, label: value.model, provider: value.provider }, ...base];
    }
    return base;
  }, [filtered, hasCurrent, value.model, value.provider]);

  return (
    <div
      className={cn(
        "llm-picker",
        compact && "is-compact",
        variant === "inline" && "is-inline",
        className
      )}
      data-testid="carousel-llm-picker"
    >
      <div className="llm-picker-row" role="group" aria-label="Carousel run LLM">
        <label className="llm-picker-field">
          <span className="llm-picker-label">Provider</span>
          <select
            className="studio-select llm-picker-select"
            value={value.provider}
            disabled={disabled}
            onChange={(event) => {
              const provider = event.target.value as CarouselRunConfig["provider"];
              const model = defaultModelForProvider(provider, models);
              const option = models.find((row) => row.id === model);
              onChange({
                provider: (option?.provider as CarouselRunConfig["provider"]) || provider,
                model,
              });
            }}
            aria-label="Carousel run LLM provider"
          >
            {providerChoices.map((provider) => (
              <option key={provider.id} value={provider.id}>
                {provider.label}
              </option>
            ))}
          </select>
        </label>
        <div className="llm-picker-field llm-picker-field-model">
          <span className="llm-picker-label">Model</span>
          <SearchableModelSelect
            value={value.model}
            options={modelOptions}
            routeProvider={value.provider}
            disabled={disabled}
            onChange={(model) => {
              const option = models.find((row) => row.id === model);
              const nextProvider = (option?.provider || value.provider) as CarouselRunConfig["provider"];
              onChange({ provider: nextProvider, model });
            }}
          />
        </div>
        {showHint ? (
          <span className="llm-picker-muted">Next generate uses this</span>
        ) : null}
      </div>

      {showGenerate && onGenerate ? (
        <button
          type="button"
          className="llm-picker-generate"
          disabled={disabled || generateBusy}
          onClick={() => void onGenerate(value)}
          data-testid="carousel-llm-generate"
        >
          <Sparkles size={14} aria-hidden />
          {generateBusy ? "Generating…" : "Generate"}
        </button>
      ) : null}
    </div>
  );
}
