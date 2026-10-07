import assert from "node:assert/strict";
import test from "node:test";

import {
  conversationSelectionExists,
  conversationStateAfterDelete,
  groupConversationsByRecency,
  synchronizeConversationSelection,
  validConversationTitle,
} from "./conversations.mjs";

const conversations = [
  { id: "first", title: "First" },
  { id: "second", title: "Second" },
  { id: "third", title: "Third" },
];

test("conversation selection updates the immediate reference and rendered state together", () => {
  const reference = { current: "old" };
  let rendered = "old";
  const selected = synchronizeConversationSelection(
    reference,
    (value) => { rendered = value; },
    "new",
  );
  assert.equal(selected, "new");
  assert.equal(reference.current, "new");
  assert.equal(rendered, "new");
});

test("a persisted conversation selection is used only when it belongs to this workspace", () => {
  assert.equal(conversationSelectionExists(conversations, "second"), true);
  assert.equal(conversationSelectionExists(conversations, 2), false);
  assert.equal(conversationSelectionExists([{ id: 2 }], "2"), true);
  assert.equal(conversationSelectionExists([], "stale-from-another-workspace"), false);
  assert.equal(conversationSelectionExists(conversations, null), false);
});

test("deleting the selected conversation chooses the next available item", () => {
  const result = conversationStateAfterDelete(conversations, "first", "first");
  assert.deepEqual(result.conversations.map((item) => item.id), ["second", "third"]);
  assert.equal(result.selectedId, "second");
  assert.equal(result.selectedWasDeleted, true);
});

test("deleting from the middle prefers the following conversation", () => {
  const result = conversationStateAfterDelete(conversations, "second", "second");
  assert.equal(result.selectedId, "third");
});

test("deleting the final conversation returns a clean selection", () => {
  const result = conversationStateAfterDelete([{ id: "only" }], "only", "only");
  assert.deepEqual(result.conversations, []);
  assert.equal(result.selectedId, null);
});

test("deleting another conversation preserves the current selection", () => {
  const result = conversationStateAfterDelete(conversations, "second", "first");
  assert.equal(result.selectedId, "first");
  assert.equal(result.selectedWasDeleted, false);
});

test("a stale delete result does not move the current selection", () => {
  const result = conversationStateAfterDelete(conversations, "missing", "second");
  assert.equal(result.conversations, conversations);
  assert.equal(result.selectedId, "second");
  assert.equal(result.selectedWasDeleted, false);
});

test("conversation titles are trimmed and bounded", () => {
  assert.equal(validConversationTitle("  Renamed conversation  "), "Renamed conversation");
  assert.equal(validConversationTitle("   "), null);
  assert.equal(validConversationTitle("x".repeat(80)), "x".repeat(80));
  assert.equal(validConversationTitle("x".repeat(81)), null);
  assert.equal(validConversationTitle("two\nlines"), null);
  assert.equal(validConversationTitle("bad\u007fcontrol"), null);
});

test("conversation history creates only populated recency groups", () => {
  const grouped = groupConversationsByRecency([
    { id: "today", updated_at: "2026-07-26T08:00:00" },
    { id: "older", updated_at: "2026-07-20T08:00:00" },
  ], new Date("2026-07-26T12:00:00"));
  assert.deepEqual(grouped.map((group) => group.label), ["Today", "20 Jul"]);
  assert.deepEqual(grouped[0].items.map((item) => item.id), ["today"]);
});

test("midnight boundaries label yesterday accurately and older dates explicitly", () => {
  const groups = groupConversationsByRecency([
    { id:"last-year",updated_at:"2025-12-31T23:50:00" },
    { id:"yesterday",updated_at:"2026-09-06T23:59:00" },
    { id:"today",updated_at:"2026-09-07T00:00:00" },
    { id:"older",updated_at:"2026-09-05T12:00:00" },
  ],new Date("2026-09-07T00:01:00"));
  assert.deepEqual(groups.map(g=>g.label),["Today","Yesterday","5 Sept","31 Dec 2025"]);
});
