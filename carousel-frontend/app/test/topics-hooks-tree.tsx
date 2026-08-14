"use client";

/**
 * /test fork of TopicsHooksTree.
 * Topics → hooks only (subtopics flattened onto the parent).
 * Toolbar (shuffle + previous saves) stays visible — shuffle is user-initiated.
 * Does not change the main carousel tree.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, History, ImageIcon, Play, Shuffle, Sparkles } from "lucide-react";
import {
  apiClient,
  formatApiError,
  type CarouselGenerationSaveListItem,
  type CarouselItemFeedback,
  type CarouselItemReference,
  type CarouselPipelineExtractResponse,
  type CarouselTopicTreeNode,
  type CarouselVerbatimItem,
} from "@/lib/api";
import { LoadingLabel } from "@/components/ui";
import { ItemFeedback } from "@/components/item-feedback";
import { ItemReferences } from "@/components/item-references";
import { useDismissible } from "@/lib/use-dismissible";
import { cn } from "@/lib/utils";
import { topicTreeFromExtract, TranscriptFramePicker } from "@/app/carousel/topics-hooks-tree";

function fmtTs(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/** Topics → hooks only. Subtopic hooks fold onto the parent; no subtopic rows. */
export function flattenTopicsForTest(
  extract: Pick<CarouselPipelineExtractResponse, "topic_tree" | "topics" | "hooks">
): CarouselTopicTreeNode[] {
  return topicTreeFromExtract(extract).map((topic) => {
    const subHooks = (topic.subtopics ?? []).flatMap((s) => s.hooks ?? []);
    const seen = new Set<string>();
    const hooks = [...(topic.hooks ?? []), ...subHooks].filter((h) => {
      const key = (h.text || "").trim().toLowerCase();
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    return { ...topic, hooks, subtopics: [] };
  });
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
  const tree = useMemo(() => flattenTopicsForTest(extract), [extract]);

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
    const task = window.setTimeout(() => setOpenTopics(init), 0);
    return () => window.clearTimeout(task);
  }, [tree]);

  useEffect(() => {
    let cancelled = false;
    const task = window.setTimeout(() => setLoadingSaves(true), 0);
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
      window.clearTimeout(task);
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
          data-testid="test-shuffle-picks"
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
          const topicKey = topic.id || topic.text;
          const topicFbKey = `hook:${topicKey}`;
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
              <ItemFeedback
                driveFileId={driveFileId}
                kind="hook"
                targetKey={topicKey}
                targetLabel={topic.text}
                initial={feedbackByKey?.[topicFbKey] ?? null}
                onSaved={onFeedbackSaved}
              />
              <ItemReferences
                driveFileId={driveFileId}
                kind="hook"
                targetKey={topicKey}
                targetLabel={topic.text}
                frameStartSec={topic.start_sec}
                frameEndSec={topic.end_sec}
                items={referencesByKey?.[topicFbKey] ?? []}
                onAdded={onReferenceAdded}
                onRemoved={onReferenceRemoved}
              />

              {open && (
                <div className="topics-hooks-children">
                  <HookLeaves
                    driveFileId={driveFileId}
                    hooks={topic.hooks ?? []}
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

      {(() => {
        const attached = new Set<string>();
        tree.forEach((t) => {
          (t.hooks ?? []).forEach((h) => attached.add(h.id));
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
  const seen = new Set<string>();
  const unique = hooks.filter((h) => {
    const key = (h.text || "").trim().toLowerCase();
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
