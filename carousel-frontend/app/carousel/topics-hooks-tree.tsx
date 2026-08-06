"use client";

/**
 * Topics → Subtopics → Hooks tree for carousel phase 3 only.
 * Does not own page chrome / phases above.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, History, ImageIcon, Play, Shuffle, Sparkles } from "lucide-react";
import {
  apiAssetUrl,
  apiClient,
  formatApiError,
  type CarouselGenerationSaveListItem,
  type CarouselItemFeedback,
  type CarouselItemReference,
  type CarouselPipelineExtractResponse,
  type CarouselTopicTreeNode,
  type CarouselTranscriptFrameItem,
  type CarouselVerbatimItem,
} from "@/lib/api";
import { LoadingLabel } from "@/components/ui";
import { ItemFeedback } from "@/components/item-feedback";
import { ItemReferences } from "@/components/item-references";
import { useDismissible } from "@/lib/use-dismissible";
import { cn } from "@/lib/utils";

function fmtTs(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/** Build topic → subtopic → hook tree from flat extract arrays (current generation). */
export function buildTreeFromFlat(
  topics: CarouselVerbatimItem[],
  hooks: CarouselVerbatimItem[]
): CarouselTopicTreeNode[] {
  const roots = topics.filter((t) => !t.is_subtopic && !t.parent_topic_id);
  const subs = topics.filter((t) => t.is_subtopic || t.parent_topic_id);
  return roots.map((t) => {
    const children = subs
      .filter((s) => s.parent_topic_id === t.id)
      .map((s) => ({
        id: s.id,
        text: s.text,
        start_sec: s.start_sec,
        end_sec: s.end_sec,
        explanation: s.explanation,
        hooks: hooks.filter((h) => h.subtopic_id === s.id || h.topic_id === s.id),
      }));
    const ownHooks = hooks.filter(
      (h) =>
        (h.topic_id === t.id || h.topic_text === t.text) &&
        !h.subtopic_id &&
        !children.some((c) => c.hooks?.some((x) => x.id === h.id))
    );
    return {
      id: t.id,
      text: t.text,
      start_sec: t.start_sec,
      end_sec: t.end_sec,
      explanation: t.explanation,
      subtopics: children,
      hooks: ownHooks,
    };
  });
}

/** Prefer live `topic_tree` from the current extract response; else rebuild from flats. */
export function topicTreeFromExtract(
  extract: Pick<CarouselPipelineExtractResponse, "topic_tree" | "topics" | "hooks">
): CarouselTopicTreeNode[] {
  if (extract.topic_tree?.length) return extract.topic_tree;
  return buildTreeFromFlat(extract.topics ?? [], extract.hooks ?? []);
}

export function TopicsHooksTree({
  driveFileId,
  extract,
  selectedHooks,
  selectedTopics,
  onToggleHook,
  onToggleTopic,
  onPreview,
  onRestoreExtract,
  onFramePicked,
  feedbackByKey,
  onFeedbackSaved,
  referencesByKey,
  onReferenceAdded,
  onReferenceRemoved,
}: {
  driveFileId: string;
  extract: CarouselPipelineExtractResponse;
  selectedHooks: string[];
  selectedTopics: string[];
  onToggleHook: (text: string) => void;
  onToggleTopic: (text: string) => void;
  onPreview: (item: { start_sec: number; text: string }) => void;
  onRestoreExtract: (
    next: CarouselPipelineExtractResponse,
    selectedHooks: string[],
    selectedTopics: string[]
  ) => void;
  onFramePicked?: (hookText: string, frameTs: number, previewUrl: string) => void;
  feedbackByKey?: Record<string, CarouselItemFeedback>;
  onFeedbackSaved?: (item: CarouselItemFeedback) => void;
  referencesByKey?: Record<string, CarouselItemReference[]>;
  onReferenceAdded?: (item: CarouselItemReference) => void;
  onReferenceRemoved?: (id: number) => void;
}) {
  const tree = useMemo(() => topicTreeFromExtract(extract), [extract]);

  const [openTopics, setOpenTopics] = useState<Record<string, boolean>>({});
  const [saves, setSaves] = useState<CarouselGenerationSaveListItem[]>([]);
  const [loadingSaves, setLoadingSaves] = useState(false);
  const [loadingShuffle, setLoadingShuffle] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const historyRef = useRef<HTMLDivElement>(null);
  useDismissible(historyOpen, () => setHistoryOpen(false), historyRef);
  const [error, setError] = useState<string | null>(null);
  const [framePick, setFramePick] = useState<{
    hookText: string;
    start_sec: number;
    end_sec?: number | null;
  } | null>(null);

  useEffect(() => {
    const init: Record<string, boolean> = {};
    tree.forEach((t, i) => {
      init[t.id] = i < 3;
    });
    setOpenTopics(init);
  }, [tree]);

  useEffect(() => {
    let cancelled = false;
    setLoadingSaves(true);
    apiClient
      .carouselPipelineSaves(driveFileId, 12, "topics_hooks")
      .then((res) => {
        if (!cancelled) setSaves(res.items ?? []);
      })
      .catch(() => {
        if (!cancelled) setSaves([]);
      })
      .finally(() => {
        if (!cancelled) setLoadingSaves(false);
      });
    return () => {
      cancelled = true;
    };
  }, [driveFileId, extract.save_id]);

  async function shuffle() {
    setLoadingShuffle(true);
    setError(null);
    try {
      const res = await apiClient.carouselPipelineShuffle({
        topic_tree: tree,
        hooks: extract.hooks,
        topics: extract.topics,
        count_hooks: 3,
        count_topics: 3,
      });
      onRestoreExtract(extract, res.selected_hooks ?? [], res.selected_topics ?? []);
    } catch (e) {
      setError(formatApiError(e, "Shuffle failed"));
    } finally {
      setLoadingShuffle(false);
    }
  }

  async function restoreSave(id: number) {
    setError(null);
    try {
      const res = await apiClient.carouselPipelineSaveGet(id);
      const payload = res.payload;
      const next: CarouselPipelineExtractResponse = {
        ...extract,
        ...payload,
        hooks: payload.hooks ?? extract.hooks,
        topics: payload.topics ?? extract.topics,
        topic_tree: payload.topic_tree ?? extract.topic_tree,
        intent: payload.intent ?? extract.intent,
        intent_score: payload.intent_score ?? extract.intent_score,
        save_id: res.id,
      };
      // Only a save the user explicitly made carries their picks; autosaves restore unselected.
      const userSave = res.source === "user_save";
      onRestoreExtract(
        next,
        userSave ? (payload.selected_hooks ?? []) : [],
        userSave ? (payload.selected_topics ?? []) : []
      );
      setHistoryOpen(false);
    } catch (e) {
      setError(formatApiError(e, "Could not restore save"));
    }
  }

  return (
    <div className="topics-hooks-tree" data-testid="topics-hooks-tree">
      <div className="topics-hooks-toolbar">
        <button
          type="button"
          className="studio-btn studio-btn-ghost"
          onClick={(e) => {
            e.preventDefault();
            void shuffle();
          }}
          disabled={loadingShuffle || loadingSaves || !tree.length}
          title={
            loadingShuffle
              ? "Shuffling…"
              : !tree.length
                ? "Nothing to shuffle yet"
                : "Reshuffle selected hooks and topics"
          }
        >
          <Shuffle size={14} />
          {loadingShuffle ? <LoadingLabel>Shuffling…</LoadingLabel> : "Shuffle picks"}
        </button>
        <div className="topics-hooks-history" ref={historyRef}>
          <button
            type="button"
            className="studio-btn studio-btn-ghost"
            onClick={() => setHistoryOpen((v) => !v)}
            aria-expanded={historyOpen}
          >
            <History size={14} />
            Previous saves
            <ChevronDown size={14} className={cn(historyOpen && "rotate-180")} />
          </button>
          {historyOpen && (
            <div className="topics-hooks-history-panel" role="listbox">
              {loadingSaves && (
                <p className="text-xs text-muted-foreground px-2 py-2">Loading saves…</p>
              )}
              {!loadingSaves && saves.length === 0 && (
                <p className="text-xs text-muted-foreground px-2 py-2">No previous saves yet.</p>
              )}
              {saves.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  className="topics-hooks-history-item"
                  onClick={() => void restoreSave(s.id)}
                >
                  <span className="font-medium text-foreground line-clamp-1">
                    {s.label || `Save #${s.id}`}
                  </span>
                  <span className="text-[10px] text-muted-foreground">
                    {s.created_at ? new Date(s.created_at).toLocaleString() : ""} ·{" "}
                    {s.topic_count ?? 0} topics · {s.hook_count ?? 0} hooks
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
        {extract.save_id ? (
          <span className="topics-hooks-autosave">Autosaved #{extract.save_id}</span>
        ) : null}
      </div>

      {error && (
        <p className="mt-2 text-xs font-medium text-destructive" role="alert">
          {error}
        </p>
      )}

      <ul className="topics-hooks-root mt-4 space-y-2">
        {tree.length === 0 && (
          <li className="text-xs text-muted-foreground">No topics in this theme window.</li>
        )}
        {tree.map((topic, ti) => {
          const topicOn = selectedTopics.includes(topic.text);
          const open = openTopics[topic.id] ?? false;
          return (
            <li
              key={topic.id}
              className="topics-hooks-topic"
              style={{ animationDelay: `${ti * 40}ms` }}
            >
              <div className="topics-hooks-topic-row">
                <button
                  type="button"
                  className="topics-hooks-chevron"
                  aria-label={open ? "Collapse" : "Expand"}
                  onClick={() => setOpenTopics((m) => ({ ...m, [topic.id]: !open }))}
                >
                  <ChevronDown size={16} className={cn("transition", open ? "" : "-rotate-90")} />
                </button>
                <button
                  type="button"
                  role="checkbox"
                  aria-checked={topicOn}
                  data-testid="topics-hooks-topic"
                  className={cn("topics-hooks-node", topicOn && "is-selected")}
                  onClick={() => onToggleTopic(topic.text)}
                >
                  <span className="topics-hooks-check" aria-hidden>
                    {topicOn ? <Check size={12} strokeWidth={2.5} /> : null}
                  </span>
                  <span className="topics-hooks-node-body">
                    <span className="topics-hooks-kind">Topic</span>
                    <span className="topics-hooks-title">{topic.text}</span>
                    <span className="topics-hooks-meta tabular-nums">
                      {fmtTs(topic.start_sec)}
                      {topic.end_sec != null ? `–${fmtTs(topic.end_sec)}` : ""}
                    </span>
                    {topic.explanation ? (
                      <span className="topics-hooks-explain">{topic.explanation}</span>
                    ) : null}
                  </span>
                </button>
                <button
                  type="button"
                  className="studio-btn studio-btn-ghost shrink-0 px-2"
                  aria-label="Preview topic"
                  onClick={() => onPreview({ start_sec: topic.start_sec, text: topic.text })}
                >
                  <Play size={14} />
                </button>
              </div>

              {open && (
                <div className="topics-hooks-children">
                  {(topic.subtopics ?? []).map((sub, si) => {
                    const subOn = selectedTopics.includes(sub.text);
                    return (
                      <div
                        key={sub.id}
                        className="topics-hooks-sub"
                        style={{ animationDelay: `${si * 35}ms` }}
                      >
                        <div className="topics-hooks-topic-row">
                          <span className="topics-hooks-rail" aria-hidden />
                          <button
                            type="button"
                            role="checkbox"
                            aria-checked={subOn}
                            data-testid="topics-hooks-subtopic"
                            className={cn("topics-hooks-node is-sub", subOn && "is-selected")}
                            onClick={() => onToggleTopic(sub.text)}
                          >
                            <span className="topics-hooks-check" aria-hidden>
                              {subOn ? <Check size={12} strokeWidth={2.5} /> : null}
                            </span>
                            <span className="topics-hooks-node-body">
                              <span className="topics-hooks-kind">Subtopic</span>
                              <span className="topics-hooks-title">{sub.text}</span>
                              <span className="topics-hooks-meta tabular-nums">
                                {fmtTs(sub.start_sec)}
                              </span>
                            </span>
                          </button>
                          <button
                            type="button"
                            className="studio-btn studio-btn-ghost shrink-0 px-2"
                            onClick={() =>
                              onPreview({ start_sec: sub.start_sec, text: sub.text })
                            }
                          >
                            <Play size={14} />
                          </button>
                        </div>
                        <HookLeaves
                          driveFileId={driveFileId}
                          hooks={sub.hooks ?? []}
                          selectedHooks={selectedHooks}
                          onToggleHook={onToggleHook}
                          onPreview={onPreview}
                          onPickFrame={(h) =>
                            setFramePick({
                              hookText: h.text,
                              start_sec: h.start_sec,
                              end_sec: h.end_sec,
                            })
                          }
                          feedbackByKey={feedbackByKey}
                          onFeedbackSaved={onFeedbackSaved}
                          referencesByKey={referencesByKey}
                          onReferenceAdded={onReferenceAdded}
                          onReferenceRemoved={onReferenceRemoved}
                        />
                      </div>
                    );
                  })}
                  <HookLeaves
                    driveFileId={driveFileId}
                    hooks={(topic.hooks ?? []).filter((h) => {
                      const key = (h.text || "").trim().toLowerCase();
                      if (!key) return false;
                      return !(topic.subtopics ?? []).some((sub) =>
                        (sub.hooks ?? []).some(
                          (sh) => (sh.text || "").trim().toLowerCase() === key
                        )
                      );
                    })}
                    selectedHooks={selectedHooks}
                    onToggleHook={onToggleHook}
                    onPreview={onPreview}
                    onPickFrame={(h) =>
                      setFramePick({
                        hookText: h.text,
                        start_sec: h.start_sec,
                        end_sec: h.end_sec,
                      })
                    }
                    feedbackByKey={feedbackByKey}
                    onFeedbackSaved={onFeedbackSaved}
                    referencesByKey={referencesByKey}
                    onReferenceAdded={onReferenceAdded}
                    onReferenceRemoved={onReferenceRemoved}
                  />
                </div>
              )}
            </li>
          );
        })}
      </ul>

      {/* Orphan hooks not attached to a topic */}
      {(() => {
        const attached = new Set<string>();
        tree.forEach((t) => {
          (t.hooks ?? []).forEach((h) => attached.add(h.id));
          (t.subtopics ?? []).forEach((s) =>
            (s.hooks ?? []).forEach((h) => attached.add(h.id))
          );
        });
        const orphans = (extract.hooks ?? []).filter((h) => !attached.has(h.id));
        if (!orphans.length) return null;
        return (
          <div className="mt-4">
            <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
              Other hooks
            </p>
            <HookLeaves
              driveFileId={driveFileId}
              hooks={orphans}
              selectedHooks={selectedHooks}
              onToggleHook={onToggleHook}
              onPreview={onPreview}
              onPickFrame={(h) =>
                setFramePick({
                  hookText: h.text,
                  start_sec: h.start_sec,
                  end_sec: h.end_sec,
                })
              }
              feedbackByKey={feedbackByKey}
              onFeedbackSaved={onFeedbackSaved}
              referencesByKey={referencesByKey}
              onReferenceAdded={onReferenceAdded}
              onReferenceRemoved={onReferenceRemoved}
            />
          </div>
        );
      })()}

      {framePick && (
        <TranscriptFramePicker
          driveFileId={driveFileId}
          startSec={framePick.start_sec}
          endSec={framePick.end_sec}
          hookText={framePick.hookText}
          onClose={() => setFramePick(null)}
          onPick={(item) => {
            onFramePicked?.(framePick.hookText, item.frame_ts, item.preview_url);
            setFramePick(null);
          }}
        />
      )}
    </div>
  );
}

function HookLeaves({
  driveFileId,
  hooks,
  selectedHooks,
  onToggleHook,
  onPreview,
  onPickFrame,
  feedbackByKey,
  onFeedbackSaved,
  referencesByKey,
  onReferenceAdded,
  onReferenceRemoved,
}: {
  driveFileId: string;
  hooks: CarouselVerbatimItem[];
  selectedHooks: string[];
  onToggleHook: (text: string) => void;
  onPreview: (item: { start_sec: number; text: string }) => void;
  onPickFrame: (hook: CarouselVerbatimItem) => void;
  feedbackByKey?: Record<string, CarouselItemFeedback>;
  onFeedbackSaved?: (item: CarouselItemFeedback) => void;
  referencesByKey?: Record<string, CarouselItemReference[]>;
  onReferenceAdded?: (item: CarouselItemReference) => void;
  onReferenceRemoved?: (id: number) => void;
}) {
  if (!hooks.length) return null;
  // Never show the same hook line twice in one section (old payloads
  // used to copy parent hooks into every subtopic).
  const seen = new Set<string>();
  const unique = hooks.filter((h) => {
    const key = (h.text || "").trim().toLowerCase();
    // Drop filler / near-empty lines that aren't carousel-worthy.
    const cleaned = key.replace(/^>+|\s+/g, " ").trim();
    if (!cleaned || cleaned.length < 12 || cleaned.split(/\s+/).length < 3) return false;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  return (
    <ul className="topics-hooks-hooks">
      {unique.map((h, i) => {
        const on = selectedHooks.includes(h.text);
        const targetKey = h.id || h.text;
        const fbKey = `hook:${targetKey}`;
        return (
          <li
            key={h.id}
            className="topics-hooks-hook"
            style={{ animationDelay: `${i * 30}ms` }}
          >
            <div className="topics-hooks-topic-row">
            <span className="topics-hooks-rail is-hook" aria-hidden />
            <button
              type="button"
              role="checkbox"
              aria-checked={on}
              data-testid="topics-hooks-hook"
              className={cn("topics-hooks-node is-hook", on && "is-selected")}
              onClick={() => onToggleHook(h.text)}
            >
              <span className="topics-hooks-check" aria-hidden>
                {on ? <Check size={12} strokeWidth={2.5} /> : null}
              </span>
              <span className="topics-hooks-node-body">
                <span className="topics-hooks-kind">
                  <Sparkles size={10} className="inline -mt-0.5 mr-1 opacity-60" />
                  Hook
                </span>
                <span className="topics-hooks-title">“{h.text}”</span>
                <span className="topics-hooks-meta tabular-nums">{fmtTs(h.start_sec)}</span>
                {h.original_text && h.original_text !== h.text ? (
                  <span className="topics-hooks-explain italic">From: {h.original_text}</span>
                ) : null}
              </span>
            </button>
            <button
              type="button"
              className="studio-btn studio-btn-ghost shrink-0 px-2"
              aria-label="Choose frame from transcript"
              title="Choose image from transcript"
              onClick={() => onPickFrame(h)}
            >
              <ImageIcon size={14} />
            </button>
            <button
              type="button"
              className="studio-btn studio-btn-ghost shrink-0 px-2"
              aria-label="Preview"
              onClick={() => onPreview({ start_sec: h.start_sec, text: h.text })}
            >
              <Play size={14} />
            </button>
            </div>
            <ItemFeedback
              driveFileId={driveFileId}
              kind="hook"
              targetKey={targetKey}
              targetLabel={h.text}
              initial={feedbackByKey?.[fbKey] ?? null}
              onSaved={onFeedbackSaved}
            />
            <ItemReferences
              driveFileId={driveFileId}
              kind="hook"
              targetKey={targetKey}
              targetLabel={h.text}
              frameStartSec={h.start_sec}
              frameEndSec={h.end_sec}
              items={referencesByKey?.[fbKey] ?? []}
              onAdded={onReferenceAdded}
              onRemoved={onReferenceRemoved}
            />
          </li>
        );
      })}
    </ul>
  );
}

export function TranscriptFramePicker({
  driveFileId,
  startSec,
  endSec,
  hookText,
  onClose,
  onPick,
}: {
  driveFileId: string;
  startSec: number;
  endSec?: number | null;
  hookText: string;
  onClose: () => void;
  onPick: (item: CarouselTranscriptFrameItem) => void;
}) {
  const [items, setItems] = useState<CarouselTranscriptFrameItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    apiClient
      .carouselTranscriptFrames({
        driveFileId,
        // Tight window: backend prefers cached frames; wide pads only force cold extracts.
        startSec: Math.max(0, startSec - 4),
        endSec: endSec != null ? endSec + 4 : startSec + 28,
        limit: 24,
      })
      .then((res) => {
        if (!cancelled) {
          const list = res.items ?? [];
          // Cached first so the grid paints immediately.
          setItems(
            [...list].sort((a, b) => {
              const ac = a.cached === false ? 1 : 0;
              const bc = b.cached === false ? 1 : 0;
              if (ac !== bc) return ac - bc;
              return a.frame_ts - b.frame_ts;
            })
          );
        }
      })
      .catch((e) => {
        if (!cancelled) setErr(formatApiError(e, "Could not load transcript frames"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [driveFileId, startSec, endSec]);

  return (
    <div className="topics-hooks-frame-overlay" role="dialog" aria-modal="true">
      <div className="topics-hooks-frame-panel">
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
              Choose image from transcript
            </p>
            <p className="mt-1 text-sm font-medium text-foreground line-clamp-2">“{hookText}”</p>
          </div>
          <button type="button" className="studio-btn studio-btn-ghost" onClick={onClose}>
            Close
          </button>
        </div>
        {loading && (
          <p className="mt-4 text-sm text-muted-foreground">
            <LoadingLabel>Loading frames…</LoadingLabel>
          </p>
        )}
        {err && (
          <p className="mt-4 text-xs text-destructive" role="alert">
            {err}
          </p>
        )}
        {!loading && !err && !items.length && (
          <p className="mt-4 text-sm text-muted-foreground">
            No transcript frames in this span yet.
          </p>
        )}
        <ul className="topics-hooks-frame-grid mt-4">
          {items.map((item) => (
            <li key={`${item.frame_ts}-${item.text.slice(0, 12)}`}>
              <button
                type="button"
                className="topics-hooks-frame-card"
                title={
                  item.cached === false
                    ? "Frame is extracted from the video on first view"
                    : undefined
                }
                onClick={() => onPick(item)}
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={apiAssetUrl(item.preview_url)}
                  alt=""
                  className="topics-hooks-frame-img"
                  loading="lazy"
                />
                <span className="topics-hooks-frame-ts tabular-nums">{fmtTs(item.start_sec)}</span>
                <span className="topics-hooks-frame-cue line-clamp-2">{item.text}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
