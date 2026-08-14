"use client";

/**
 * Topic → Hook flow (/test/studio only).
 *
 * Design read: product selection UI for creators — Linear-minimal language,
 * matching the Excalidraw two-column sketch. Dials: VARIANCE 5 / MOTION 3 /
 * DENSITY 3 (calm, airy, low motion).
 *
 * Layout: one row per topic — large topic cell on the left, thin hook bars
 * on the right, joined by a single curve. Selected hooks expand to show an
 * editable Copy textarea plus feedback/refs. Timestamps/ids stay fixed.
 */

import { ThumbsDown, ThumbsUp } from "lucide-react";
import { cn } from "@/lib/utils";
import type { TestItem } from "@/lib/test-api";

function fmtTs(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function hookEditKey(item: TestItem): string {
  return item.id || item.text;
}

export type DiagramTopic = TestItem & { hooks: TestItem[] };

/** Merge each root topic with every hook crafted for it or any of its subtopics. */
export function buildDiagramTopics(
  topics: TestItem[],
  hooks: TestItem[]
): { topics: DiagramTopic[]; orphans: TestItem[] } {
  const roots = topics
    .filter((t) => !t.is_subtopic && !t.parent_topic_id)
    .slice()
    .sort((a, b) => a.start_sec - b.start_sec);

  const subsByParent = new Map<string, string[]>();
  for (const t of topics) {
    if ((t.is_subtopic || t.parent_topic_id) && t.parent_topic_id) {
      const arr = subsByParent.get(t.parent_topic_id) ?? [];
      arr.push(t.id);
      subsByParent.set(t.parent_topic_id, arr);
    }
  }

  const list: DiagramTopic[] = roots.map((t) => {
    const subIds = new Set(subsByParent.get(t.id) ?? []);
    const seen = new Set<string>();
    const own = hooks.filter((h) => {
      const belongs =
        h.topic_id === t.id ||
        (!!h.topic_id && subIds.has(h.topic_id)) ||
        (!!h.subtopic_id && subIds.has(h.subtopic_id)) ||
        (!h.topic_id && h.topic_text === t.text);
      if (!belongs || seen.has(h.id)) return false;
      seen.add(h.id);
      return true;
    });
    return { ...t, hooks: own.slice().sort((a, b) => a.start_sec - b.start_sec) };
  });

  const attached = new Set(list.flatMap((t) => t.hooks.map((h) => h.id)));
  const orphans = hooks.filter((h) => !attached.has(h.id));
  return { topics: list, orphans };
}

function HookActions({
  item,
  feedback,
  refs,
  onRate,
  onAddRef,
}: {
  item: TestItem;
  feedback: Record<string, "up" | "down">;
  refs: Record<string, string[]>;
  onRate: (item: TestItem, rating: "up" | "down") => void;
  onAddRef: (item: TestItem) => void;
}) {
  const key = hookEditKey(item);
  return (
    <div className="thd-hook-actions">
      <button
        type="button"
        className={cn("thd-icon-btn", feedback[key] === "up" && "is-up")}
        onClick={() => void onRate(item, "up")}
        aria-label="Thumbs up"
      >
        <ThumbsUp size={11} strokeWidth={1.75} />
      </button>
      <button
        type="button"
        className={cn("thd-icon-btn", feedback[key] === "down" && "is-down")}
        onClick={() => void onRate(item, "down")}
        aria-label="Thumbs down"
      >
        <ThumbsDown size={11} strokeWidth={1.75} />
      </button>
      <button type="button" className="thd-text-btn" onClick={() => void onAddRef(item)}>
        Add ref
      </button>
      {(refs[key] ?? []).length > 0 && (
        <span className="thd-ref-count">{refs[key].length}</span>
      )}
    </div>
  );
}

function HookCopyEditor({
  item,
  displayText,
  onChangeHook,
}: {
  item: TestItem;
  displayText: string;
  onChangeHook?: (item: TestItem, text: string) => void;
}) {
  if (!onChangeHook) return null;
  return (
    <label className="thd-hook-copy">
      <span className="thd-hook-copy-label">Copy</span>
      <textarea
        className="thd-hook-copy-input"
        rows={3}
        value={displayText}
        onChange={(e) => onChangeHook(item, e.target.value)}
        onClick={(e) => e.stopPropagation()}
        spellCheck
        data-testid="hook-copy-editor"
        aria-label="Edit hook copy"
      />
    </label>
  );
}

function HookCard({
  h,
  selected,
  displayText,
  feedback,
  refs,
  onToggleHook,
  onRate,
  onAddRef,
  onChangeHook,
}: {
  h: TestItem;
  selected: boolean;
  displayText: string;
  feedback: Record<string, "up" | "down">;
  refs: Record<string, string[]>;
  onToggleHook: (item: TestItem) => void;
  onRate: (item: TestItem, rating: "up" | "down") => void;
  onAddRef: (item: TestItem) => void;
  onChangeHook?: (item: TestItem, text: string) => void;
}) {
  return (
    <div className={cn("thd-hook", selected && "is-selected", selected && "is-expanded")}>
      <button
        type="button"
        role="checkbox"
        aria-checked={selected}
        className="thd-hook-btn"
        onClick={() => onToggleHook(h)}
        data-testid="topics-hooks-hook"
      >
        <span className="thd-hook-text">{displayText}</span>
        <span className="thd-hook-time tabular-nums">{fmtTs(h.start_sec)}</span>
      </button>
      {selected && (
        <div className="thd-hook-detail">
          <HookCopyEditor item={h} displayText={displayText} onChangeHook={onChangeHook} />
          <HookActions
            item={h}
            feedback={feedback}
            refs={refs}
            onRate={onRate}
            onAddRef={onAddRef}
          />
        </div>
      )}
    </div>
  );
}

/** Plain list for hooks that couldn't be matched to any topic — rare, kept minimal. */
export function OrphanHooks({
  hooks,
  selectedHooks,
  hookEdits,
  feedback,
  refs,
  onToggleHook,
  onRate,
  onAddRef,
  onChangeHook,
}: {
  hooks: TestItem[];
  selectedHooks: string[];
  hookEdits?: Record<string, string>;
  feedback: Record<string, "up" | "down">;
  refs: Record<string, string[]>;
  onToggleHook: (item: TestItem) => void;
  onRate: (item: TestItem, rating: "up" | "down") => void;
  onAddRef: (item: TestItem) => void;
  onChangeHook?: (item: TestItem, text: string) => void;
}) {
  if (!hooks.length) return null;
  return (
    <div className="thd-orphans">
      <p className="thd-col-label">Other hooks</p>
      <div className="thd-group">
        {hooks.map((h) => {
          const on = selectedHooks.includes(h.text);
          const displayText = hookEdits?.[hookEditKey(h)] ?? h.text;
          return (
            <HookCard
              key={h.id}
              h={h}
              selected={on}
              displayText={displayText}
              feedback={feedback}
              refs={refs}
              onToggleHook={onToggleHook}
              onRate={onRate}
              onAddRef={onAddRef}
              onChangeHook={onChangeHook}
            />
          );
        })}
      </div>
    </div>
  );
}

function FlowArrow({ active }: { active: boolean }) {
  return (
    <svg
      className={cn("thd-arrow", active && "is-active")}
      viewBox="0 0 56 40"
      preserveAspectRatio="none"
      aria-hidden
      focusable="false"
    >
      <path
        d="M 2 20 C 18 20, 38 20, 48 20"
        fill="none"
        vectorEffect="non-scaling-stroke"
      />
      <path d="M 44 14 L 52 20 L 44 26" fill="none" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export function TopicHookDiagram({
  topics,
  selectedTopics,
  selectedHooks,
  hookEdits,
  onToggleTopic,
  onToggleHook,
  feedback,
  refs,
  onRate,
  onAddRef,
  onChangeHook,
}: {
  topics: DiagramTopic[];
  selectedTopics: string[];
  selectedHooks: string[];
  hookEdits?: Record<string, string>;
  onToggleTopic: (text: string) => void;
  onToggleHook: (item: TestItem) => void;
  feedback: Record<string, "up" | "down">;
  refs: Record<string, string[]>;
  onRate: (item: TestItem, rating: "up" | "down") => void;
  onAddRef: (item: TestItem) => void;
  onChangeHook?: (item: TestItem, text: string) => void;
}) {
  if (!topics.length) {
    return <p className="thd-empty-state">No topics for this video yet.</p>;
  }

  return (
    <div className="thd-card" data-testid="topics-hooks-split">
      <div className="thd-head">
        <span className="thd-col-label">Topics</span>
        <span className="thd-col-label thd-head-hooks">Hooks</span>
      </div>

      {/* Two independently scrollable columns — max-height from CSS keeps Generate copy visible */}
      <div className="thd-split">
        <div className="thd-col thd-col-topics" role="list" aria-label="Topics">
          {topics.map((t) => {
            const topicOn = selectedTopics.includes(t.text);
            const groupActive =
              topicOn || t.hooks.some((h) => selectedHooks.includes(h.text));
            return (
              <div
                key={t.id}
                className={cn("thd-topic-slot", groupActive && "is-active")}
                role="listitem"
              >
                <button
                  type="button"
                  role="checkbox"
                  aria-checked={topicOn}
                  className={cn("thd-topic", topicOn && "is-selected")}
                  onClick={() => onToggleTopic(t.text)}
                  data-testid="topics-hooks-topic"
                >
                  <span className="thd-topic-title">{t.text}</span>
                  <span className="thd-topic-time tabular-nums">
                    {fmtTs(t.start_sec)}
                    {t.end_sec != null ? `–${fmtTs(t.end_sec)}` : ""}
                  </span>
                </button>
              </div>
            );
          })}
        </div>

        <div className="thd-col-gutter" aria-hidden>
          <div className="thd-connector thd-connector-static">
            <FlowArrow active={selectedTopics.length > 0 || selectedHooks.length > 0} />
          </div>
        </div>

        <div className="thd-col thd-col-hooks" role="list" aria-label="Hooks">
          {topics.map((t) => {
            const topicOn = selectedTopics.includes(t.text);
            const groupActive =
              topicOn || t.hooks.some((h) => selectedHooks.includes(h.text));
            return (
              <div
                key={t.id}
                className={cn("thd-group", groupActive && "is-active")}
                role="listitem"
              >
                {t.hooks.length === 0 && (
                  <p className="thd-empty">No hooks for this topic</p>
                )}
                {t.hooks.map((h) => {
                  const on = selectedHooks.includes(h.text);
                  const displayText = hookEdits?.[hookEditKey(h)] ?? h.text;
                  return (
                    <HookCard
                      key={h.id}
                      h={h}
                      selected={on}
                      displayText={displayText}
                      feedback={feedback}
                      refs={refs}
                      onToggleHook={onToggleHook}
                      onRate={onRate}
                      onAddRef={onAddRef}
                      onChangeHook={onChangeHook}
                    />
                  );
                })}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
