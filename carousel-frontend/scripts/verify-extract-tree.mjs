/**
 * Prove extract → tree mapping for the Step 3 hooks/topics UI.
 * Run: node scripts/verify-extract-tree.mjs
 */
import assert from "node:assert/strict";

function buildTreeFromFlat(topics, hooks) {
  const roots = topics.filter((t) => !t.is_subtopic && !t.parent_topic_id);
  const subs = topics.filter((t) => t.is_subtopic || t.parent_topic_id);
  return roots.map((t) => {
    const children = subs
      .filter((s) => s.parent_topic_id === t.id)
      .map((s) => ({
        id: s.id,
        text: s.text,
        start_sec: s.start_sec,
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
      subtopics: children,
      hooks: ownHooks,
    };
  });
}

function topicTreeFromExtract(extract) {
  if (extract.topic_tree?.length) return extract.topic_tree;
  return buildTreeFromFlat(extract.topics ?? [], extract.hooks ?? []);
}

// Current-generation shaped payload (mirrors /pipeline/extract + autosave).
const liveExtract = {
  topic_tree: [
    {
      id: "topic_1",
      text: "Indian Oil's National Fueling Mission",
      start_sec: 0,
      subtopics: [
        {
          id: "sub_1",
          text: "Scale of crude dependence",
          start_sec: 12,
          hooks: [
            {
              id: "hook_1",
              text: "Over 55% of India runs on Indian Oil.",
              start_sec: 20,
            },
          ],
        },
      ],
      hooks: [],
    },
    {
      id: "topic_2",
      text: "Crude oil as economic bloodstream",
      start_sec: 45,
      subtopics: [],
      hooks: [
        {
          id: "hook_2",
          text: "Crude is the silent partner in every journey.",
          start_sec: 50,
        },
      ],
    },
  ],
  topics: [],
  hooks: [],
};

const tree = topicTreeFromExtract(liveExtract);
assert.equal(tree.length, 2, "tree should expose topics from current extract");
assert.equal(tree[0].text, "Indian Oil's National Fueling Mission");
assert.equal(tree[0].subtopics?.[0]?.hooks?.length, 1, "nested hooks under subtopic");
assert.equal(tree[1].hooks?.length, 1, "hooks under topic");

// Flat fallback when topic_tree missing (still current generation fields).
const flatOnly = topicTreeFromExtract({
  topics: [
    { id: "topic_1", text: "Root A", start_sec: 0 },
    { id: "topic_2", text: "Child A", start_sec: 5, is_subtopic: true, parent_topic_id: "topic_1" },
  ],
  hooks: [
    { id: "hook_1", text: "Hook on child", start_sec: 6, topic_id: "topic_2", subtopic_id: "topic_2" },
  ],
});
assert.equal(flatOnly.length, 1);
assert.equal(flatOnly[0].subtopics?.length, 1);
assert.equal(flatOnly[0].subtopics?.[0]?.hooks?.length, 1);

// Empty extract must not invent stale tree nodes.
assert.equal(topicTreeFromExtract({ topic_tree: [], topics: [], hooks: [] }).length, 0);

// Simulate the page state gate: extract response → phase 3 visible.
function shouldShowHooksTopics(state) {
  return Boolean(state.extract && state.selectedThemes.length > 0 && state.phase >= 3);
}
assert.equal(
  shouldShowHooksTopics({ extract: liveExtract, selectedThemes: [{ id: 1 }], phase: 3 }),
  true
);
assert.equal(
  shouldShowHooksTopics({ extract: null, selectedThemes: [{ id: 1 }], phase: 2 }),
  false,
  "themes-only until extract lands"
);

console.log("verify-extract-tree: ok");
