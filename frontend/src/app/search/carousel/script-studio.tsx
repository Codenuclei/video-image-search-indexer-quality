"use client";

import { useEffect, useMemo, useRef, useState, type MutableRefObject } from "react";
import {
  apiAssetUrl,
  apiClient,
  formatApiError,
  type CarouselCueItem,
  type CarouselPresetItem,
  type CarouselScriptTurn,
  type CarouselSnapshotContext,
  type SearchMoment,
} from "@/lib/api";
import { PresetChipMultiSelect } from "./preset-chips";
import { SnapshotCuesPanel, cueToSnapshot } from "./snapshot-cues";
import {
  formatTimestamp,
  formatTimestampRange,
  mergePresets,
  momentToSnapshot,
  slugify,
  toggleId,
} from "./utils";

export type ScriptDraft = {
  id: string;
  prompt: string;
  script: string;
  hooks: string[];
  topics: string[];
  snapshot: CarouselSnapshotContext | null;
  source: string;
  createdAt: number;
};

const FALLBACK_HOOKS: CarouselPresetItem[] = [
  { id: "curiosity_gap", label: "Curiosity gap", blurb: "Tease a reveal the viewer must stay for." },
  { id: "bold_claim", label: "Bold claim", blurb: "Open with a confident, slightly contrarian statement." },
  { id: "pain_point", label: "Relatable pain", blurb: "Name a frustration the audience already feels." },
  { id: "stat_shock", label: "Surprising stat", blurb: "Lead with a number that reframes the topic." },
  { id: "direct_question", label: "Direct question", blurb: "Ask the viewer something they want answered." },
  { id: "story_teaser", label: "Story teaser", blurb: "Start mid-scene, then rewind to explain." },
  { id: "challenge", label: "Challenge", blurb: "Dare the viewer to try one concrete action." },
];

const FALLBACK_TOPICS: CarouselPresetItem[] = [
  { id: "leadership", label: "Leadership", blurb: "Decisions, influence, and owning outcomes." },
  { id: "learning", label: "Learning & skills", blurb: "Growth, practice, and teaching moments." },
  { id: "collaboration", label: "Collaboration", blurb: "Teams, feedback, and working together." },
  { id: "innovation", label: "Innovation", blurb: "Change, experiments, and new ideas." },
  { id: "personal_brand", label: "Personal brand", blurb: "Presence, credibility, and storytelling." },
  { id: "productivity", label: "Productivity", blurb: "Focus, systems, and getting things done." },
  { id: "career", label: "Career advice", blurb: "Paths, interviews, and professional moves." },
];

export type ScriptStudioProps = {
  /** When true, hooks/topics auto-surface with reveal motion */
  active: boolean;
  moments: SearchMoment[];
  driveFileId?: string | null;
  seedQuery?: string;
  pickedSnapshot: CarouselSnapshotContext | null;
  onPickedSnapshotChange: (snap: CarouselSnapshotContext | null) => void;
  scriptPrompt: string;
  onScriptPromptChange: (value: string) => void;
  selectedHooks: string[];
  onSelectedHooksChange: (ids: string[] | ((prev: string[]) => string[])) => void;
  selectedTopics: string[];
  onSelectedTopicsChange: (ids: string[] | ((prev: string[]) => string[])) => void;
  drafts: ScriptDraft[];
  onDraftsChange: (drafts: ScriptDraft[] | ((prev: ScriptDraft[]) => ScriptDraft[])) => void;
  slideCount: number;
  onSlideCountChange: (n: number) => void;
  generating?: boolean;
  onGeneratingChange?: (v: boolean) => void;
  generateRef?: MutableRefObject<((iterate?: boolean) => Promise<ScriptDraft | null>) | null>;
  /** Outline-returned hook/topic labels to merge + auto-select */
  outlineHooks?: string[];
  outlineTopics?: string[];
  /** External theme titles from transcript (merged into topics) */
  transcriptThemes?: string[];
};

export function ScriptStudio({
  active,
  moments,
  driveFileId,
  seedQuery = "",
  pickedSnapshot,
  onPickedSnapshotChange,
  scriptPrompt,
  onScriptPromptChange,
  selectedHooks,
  onSelectedHooksChange,
  selectedTopics,
  onSelectedTopicsChange,
  drafts,
  onDraftsChange,
  slideCount,
  onSlideCountChange,
  generating: generatingProp,
  onGeneratingChange,
  generateRef,
  outlineHooks = [],
  outlineTopics = [],
  transcriptThemes = [],
}: ScriptStudioProps) {
  const [hooks, setHooks] = useState<CarouselPresetItem[]>(FALLBACK_HOOKS);
  const [topics, setTopics] = useState<CarouselPresetItem[]>(FALLBACK_TOPICS);
  const [expandingKind, setExpandingKind] = useState<"hooks" | "topics" | null>(null);
  const [scriptError, setScriptError] = useState<string | null>(null);
  const [localGenerating, setLocalGenerating] = useState(false);
  const [cues, setCues] = useState<CarouselCueItem[]>([]);
  const [cuesLoading, setCuesLoading] = useState(false);
  const [revealKey, setRevealKey] = useState(0);
  const hooksRef = useRef<HTMLDivElement>(null);
  const wasActive = useRef(false);

  const generating = generatingProp ?? localGenerating;
  const setGenerating = (v: boolean) => {
    setLocalGenerating(v);
    onGeneratingChange?.(v);
  };

  const hasSelection = selectedHooks.length > 0 || selectedTopics.length > 0;
  const latestDraft = drafts[0] ?? null;

  const historyForApi: CarouselScriptTurn[] = useMemo(
    () =>
      [...drafts]
        .reverse()
        .flatMap((d) => [
          { role: "user", content: d.prompt },
          { role: "assistant", content: d.script },
        ]),
    [drafts]
  );

  useEffect(() => {
    apiClient
      .carouselPresets()
      .then((p) => {
        if (p.hooks?.length) setHooks(p.hooks);
        if (p.topics?.length) setTopics(p.topics);
      })
      .catch(() => {
        setHooks(FALLBACK_HOOKS);
        setTopics(FALLBACK_TOPICS);
      });
  }, []);

  /* Auto-surface: when work surface becomes active, reveal hooks + scroll into view */
  useEffect(() => {
    if (active && !wasActive.current) {
      setRevealKey((k) => k + 1);
      requestAnimationFrame(() => {
        hooksRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
    }
    wasActive.current = active;
  }, [active]);

  /* Merge outline-returned hooks/topics and auto-select them */
  const outlineHookKey = outlineHooks.join("\0");
  const outlineTopicKey = outlineTopics.join("\0");
  useEffect(() => {
    if (!outlineHooks.length && !outlineTopics.length) return;

    const hookItems = outlineHooks.map((label) => ({
      id: slugify(label),
      label,
      blurb: "From generated outline",
    }));
    const topicItems = outlineTopics.map((label) => ({
      id: slugify(label),
      label,
      blurb: "From generated outline",
    }));

    if (hookItems.length) {
      setHooks((prev) => mergePresets(prev, hookItems));
      const ids = hookItems.map((h) => h.id);
      onSelectedHooksChange((prev) => Array.from(new Set([...prev, ...ids])));
      setRevealKey((k) => k + 1);
    }
    if (topicItems.length) {
      setTopics((prev) => mergePresets(prev, topicItems));
      const ids = topicItems.map((t) => t.id);
      onSelectedTopicsChange((prev) => Array.from(new Set([...prev, ...ids])));
    }
    // Only react when outline payload identity changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outlineHookKey, outlineTopicKey]);

  /* Merge transcript theme titles into selectable topics */
  const transcriptKey = transcriptThemes.join("\0");
  useEffect(() => {
    if (!transcriptThemes.length) return;
    const items = transcriptThemes.map((title) => ({
      id: `tx:${slugify(title)}`,
      label: title,
      blurb: "From transcript topics",
    }));
    setTopics((prev) => mergePresets(prev, items));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transcriptKey]);

  useEffect(() => {
    if (!hasSelection) {
      setCues([]);
      return;
    }
    let cancelled = false;
    setCuesLoading(true);
    apiClient
      .matchCarouselCues({
        hooks: selectedHooks,
        topics: selectedTopics,
        moments: moments.map(momentToSnapshot),
        drive_file_id: driveFileId || undefined,
      })
      .then((res) => {
        if (!cancelled) setCues(res.cues ?? []);
      })
      .catch(() => {
        if (!cancelled) setCues([]);
      })
      .finally(() => {
        if (!cancelled) setCuesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedHooks, selectedTopics, moments, driveFileId, hasSelection]);

  async function expandPresets(kind: "hooks" | "topics") {
    setExpandingKind(kind);
    setScriptError(null);
    try {
      const res = await apiClient.expandCarouselPresets(kind, scriptPrompt || seedQuery, 4);
      if (kind === "hooks") setHooks((prev) => mergePresets(prev, res.items));
      else setTopics((prev) => mergePresets(prev, res.items));
    } catch (e) {
      setScriptError(formatApiError(e, "Could not expand presets"));
    } finally {
      setExpandingKind(null);
    }
  }

  async function generateScript(iterateFromLatest = false): Promise<ScriptDraft | null> {
    if (!hasSelection) {
      setScriptError("Select at least one hook or topic first.");
      return null;
    }
    const basePrompt = scriptPrompt.trim();
    if (!basePrompt && !(iterateFromLatest && latestDraft)) return null;

    const prompt =
      iterateFromLatest && latestDraft
        ? basePrompt ||
          `Refine the previous draft. Keep the same hooks/topics and snapshot unless I change them.`
        : basePrompt;

    setGenerating(true);
    setScriptError(null);
    try {
      const res = await apiClient.generateCarouselScript({
        prompt,
        hooks: selectedHooks,
        topics: selectedTopics,
        snapshot: pickedSnapshot,
        history: iterateFromLatest ? historyForApi : historyForApi.slice(-8),
      });
      const draft: ScriptDraft = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        prompt,
        script: res.script,
        hooks: res.hooks,
        topics: res.topics,
        snapshot: pickedSnapshot,
        source: res.source,
        createdAt: Date.now(),
      };
      onDraftsChange((prev) => [draft, ...prev]);
      if (res.warning) setScriptError(res.warning);
      return draft;
    } catch (e) {
      setScriptError(formatApiError(e, "Script generation failed"));
      return null;
    } finally {
      setGenerating(false);
    }
  }

  useEffect(() => {
    if (generateRef) generateRef.current = generateScript;
  });

  function continueFromDraft(draft: ScriptDraft) {
    onScriptPromptChange(
      `Continue from this draft — tighten pacing and keep the same voice:\n\n${draft.script}`
    );
    if (draft.snapshot) onPickedSnapshotChange(draft.snapshot);
  }

  if (!active) return null;

  return (
    <div className="studio-hooks-reveal space-y-8 px-4 py-6 sm:px-6" data-testid="script-studio">
      <div ref={hooksRef} key={`hooks-${revealKey}`}>
        <p className="studio-section-label">Hooks</p>
        <h3 className="mt-1 text-base font-semibold tracking-tight text-foreground">
          Opening angles for this video
        </h3>
        <p className="mt-1 mb-4 max-w-xl text-sm font-medium text-muted-foreground">
          These surface automatically once you select a video. Multi-select 5–8 that fit the cut.
        </p>
        <PresetChipMultiSelect
          title="Choose hooks"
          hint="Curated openings — expand if you need more variants."
          items={hooks}
          selected={selectedHooks}
          onToggle={(id) => onSelectedHooksChange(toggleId(selectedHooks, id))}
          onExpand={() => void expandPresets("hooks")}
          expanding={expandingKind === "hooks"}
          reveal
        />
      </div>

      <div>
        <PresetChipMultiSelect
          title="Topics"
          hint="Cohesive themes the script should cover — including ones from the transcript."
          items={topics}
          selected={selectedTopics}
          onToggle={(id) => onSelectedTopicsChange(toggleId(selectedTopics, id))}
          onExpand={() => void expandPresets("topics")}
          expanding={expandingKind === "topics"}
          reveal
        />
      </div>

      {!hasSelection ? (
        <p className="border border-dashed border-input bg-muted px-4 py-5 text-sm font-medium text-muted-foreground">
          Select hooks and topics to unlock the script prompt and spoken cues.
        </p>
      ) : (
        <div className="studio-rise space-y-6">
          <div className="space-y-2">
            <label className="studio-section-label" htmlFor="script-prompt">
              Script prompt
            </label>
            <textarea
              id="script-prompt"
              className="studio-textarea"
              placeholder="How should the spoken script sound? e.g. 20-second reel that opens with a question, then lands on the insight from the selected moment."
              value={scriptPrompt}
              onChange={(e) => onScriptPromptChange(e.target.value)}
            />
          </div>

          <div className="space-y-3">
            <div>
              <p className="studio-section-label">Spoken snapshots</p>
              <p className="mt-1 text-sm font-medium text-muted-foreground">
                Cues where each selected hook or topic is spoken — pick one calmly.
              </p>
            </div>
            <SnapshotCuesPanel
              cues={cues}
              loading={cuesLoading}
              moments={moments}
              activeSnapshot={pickedSnapshot}
              onPickCue={(cue) => {
                const snap = cueToSnapshot(cue);
                if (snap) onPickedSnapshotChange(snap);
              }}
              onUseMoment={(m) => onPickedSnapshotChange(momentToSnapshot(m))}
            />
            {pickedSnapshot && (
              <div className="flex flex-wrap items-center gap-3 border-t border-border pt-3">
                <span className="text-xs font-medium text-muted-foreground">Active snapshot</span>
                {pickedSnapshot.preview_url && (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={apiAssetUrl(pickedSnapshot.preview_url)}
                    alt=""
                    className="h-8 w-14 rounded-[4px] object-cover"
                  />
                )}
                <span className="text-xs font-semibold text-foreground">
                  {formatTimestampRange(pickedSnapshot.timestamp_sec, pickedSnapshot.end_timestamp_sec)}{" "}
                  · {pickedSnapshot.name}
                </span>
                <button
                  type="button"
                  className="studio-btn studio-btn-ghost ml-auto"
                  onClick={() => onPickedSnapshotChange(null)}
                >
                  Clear
                </button>
              </div>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className="studio-btn studio-btn-primary"
              onClick={() => void generateScript(false)}
              disabled={generating || !scriptPrompt.trim()}
            >
              {generating ? "Generating…" : "Generate script"}
            </button>
            <button
              type="button"
              className="studio-btn studio-btn-secondary"
              onClick={() => void generateScript(true)}
              disabled={generating || (!latestDraft && !scriptPrompt.trim())}
            >
              Iterate
            </button>
            <label className="ml-auto flex items-center gap-2 text-xs font-medium text-muted-foreground">
              Slides
              <select
                className="studio-select !h-9 !w-auto !px-2 text-sm"
                value={slideCount}
                onChange={(e) => onSlideCountChange(Number(e.target.value))}
              >
                {[5, 6, 7, 8].map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </div>
      )}

      {scriptError && (
        <p className="text-xs font-medium text-destructive" role="alert">
          {scriptError}
        </p>
      )}

      {drafts.length > 0 && (
        <div className="space-y-4 border-t border-border pt-6">
          <div>
            <p className="studio-section-label">Draft history</p>
            <p className="mt-1 text-sm font-medium text-muted-foreground">
              Newest first. Iterate sends prior AI output as context.
            </p>
          </div>
          <ul className="space-y-4">
            {drafts.map((draft, idx) => (
              <li
                key={draft.id}
                className="border border-border bg-muted p-4"
                data-testid={`script-draft-${idx}`}
              >
                <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px] font-medium text-muted-foreground">
                  <span className="font-semibold text-foreground">
                    Draft {drafts.length - idx}
                  </span>
                  <span>{draft.source}</span>
                  {draft.hooks.length > 0 && <span>hooks: {draft.hooks.join(", ")}</span>}
                  {draft.topics.length > 0 && <span>topics: {draft.topics.join(", ")}</span>}
                  {draft.snapshot && (
                    <span>
                      @ {formatTimestamp(draft.snapshot.timestamp_sec)} · {draft.snapshot.name}
                    </span>
                  )}
                </div>
                <p className="mb-2 whitespace-pre-wrap text-xs font-medium text-muted-foreground">
                  <span className="font-semibold text-foreground">Prompt: </span>
                  {draft.prompt}
                </p>
                <pre className="whitespace-pre-wrap font-sans text-sm font-medium leading-relaxed text-foreground">
                  {draft.script}
                </pre>
                <div className="mt-3">
                  <button
                    type="button"
                    className="studio-btn studio-btn-ghost"
                    onClick={() => continueFromDraft(draft)}
                  >
                    Continue from this
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export { FALLBACK_HOOKS, FALLBACK_TOPICS };
