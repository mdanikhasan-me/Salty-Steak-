import assert from "node:assert/strict";
import test from "node:test";

import {
  conversationTitle,
  folderOf,
  matchesQuery,
  moveChoicesFor,
  openFolder,
  organiseConversations,
} from "./conversationOrganisation.mjs";

const NOW = new Date("2026-08-15T12:00:00Z");

function chat(id, overrides = {}) {
  return {
    id,
    title: id,
    updated_at: "2026-08-15T09:00:00Z",
    labels: [],
    ...overrides,
  };
}

function inFolder(id, name) {
  return { labels: [{ id, name }] };
}

test("a pinned conversation appears in Pinned and nowhere else", () => {
  const organised = organiseConversations({
    conversations: [
      chat("kept", { pinned: true, pinned_at: "2026-08-15T08:00:00Z" }),
      chat("ordinary"),
    ],
    now: NOW,
  });

  assert.deepEqual(organised.pinned.map((item) => item.id), ["kept"]);
  const recents = organised.groups.flatMap((group) => group.items.map((i) => i.id));
  assert.deepEqual(recents, ["ordinary"]);
});

test("pinned conversations are ordered by when they were pinned", () => {
  const organised = organiseConversations({
    conversations: [
      chat("older", { pinned_at: "2026-08-10T08:00:00Z" }),
      chat("newer", { pinned_at: "2026-08-14T08:00:00Z" }),
    ],
    now: NOW,
  });

  assert.deepEqual(organised.pinned.map((item) => item.id), ["newer", "older"]);
});

test("a conversation reports the one folder it is in", () => {
  assert.deepEqual(folderOf(chat("a", inFolder("f1", "Hardware"))), {
    id: "f1",
    name: "Hardware",
  });
  assert.equal(folderOf(chat("a")), null);
});

test("search looks at titles and at folder names", () => {
  const filed = chat("a", { title: "Fan curve", ...inFolder("f1", "Hardware") });

  assert.equal(matchesQuery(filed, "fan"), true);
  assert.equal(matchesQuery(filed, "hardware"), true);
  assert.equal(matchesQuery(filed, "  HARD "), true);
  assert.equal(matchesQuery(filed, "university"), false);
  assert.equal(matchesQuery(filed, "   "), true);
});

test("opening a folder shows only what is inside it", () => {
  const organised = organiseConversations({
    conversations: [
      chat("filed", inFolder("f1", "Uni")),
      chat("loose"),
    ],
    folders: [{ id: "f1", name: "Uni", conversation_count: 1 }],
    openFolderId: "f1",
    now: NOW,
  });

  const shown = organised.groups.flatMap((group) => group.items.map((i) => i.id));
  assert.deepEqual(shown, ["filed"]);
  assert.equal(organised.filtered, true);
  assert.equal(organised.visibleCount, 1);
  assert.equal(organised.total, 2);
  assert.equal(openFolder(organised).name, "Uni");
});

test("no filter reports itself as unfiltered", () => {
  const organised = organiseConversations({
    conversations: [chat("a"), chat("b")],
    now: NOW,
  });

  assert.equal(organised.filtered, false);
  assert.equal(openFolder(organised), null);
});

test("a pinned conversation is still findable by search", () => {
  const organised = organiseConversations({
    conversations: [chat("pinned-one", { title: "Fan curve", pinned: true })],
    query: "fan",
    now: NOW,
  });

  assert.deepEqual(organised.pinned.map((item) => item.id), ["pinned-one"]);
});

test("a filter that matches nothing yields empty sections, not everything", () => {
  const organised = organiseConversations({
    conversations: [chat("a"), chat("b")],
    query: "nothing matches this",
    now: NOW,
  });

  assert.deepEqual(organised.pinned, []);
  assert.deepEqual(organised.groups, []);
  assert.equal(organised.visibleCount, 0);
});

test("the move menu marks the folder the conversation is already in", () => {
  const choices = moveChoicesFor(chat("a", inFolder("f1", "Uni")), [
    { id: "f1", name: "Uni" },
    { id: "f2", name: "Hardware" },
  ]);

  assert.deepEqual(
    choices.map((item) => [item.name, item.current]),
    [["Uni", true], ["Hardware", false]],
  );
});

test("a conversation with no title still renders as something", () => {
  assert.equal(conversationTitle({ title: "   " }), "New chat");
  assert.equal(conversationTitle(null), "New chat");
  assert.equal(conversationTitle({ title: "Fan curve" }), "Fan curve");
});

test("folders without an id or a name are not rendered", () => {
  const organised = organiseConversations({
    conversations: [],
    folders: [{ id: "", name: "ghost" }, { id: "f1", name: "" }, { id: "f2", name: "Real" }],
    now: NOW,
  });

  assert.deepEqual(organised.folders.map((item) => item.name), ["Real"]);
});
