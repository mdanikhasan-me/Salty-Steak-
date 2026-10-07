import test from "node:test";
import assert from "node:assert/strict";

import {
  activityEntryTelemetry,
  currentActivityEntry,
  normaliseActivityJournal,
} from "./activityJournal.mjs";

const operation = {
  result: {
    activity_journal: [
      { id: "live", sequence: 1, label: "Reading source", state: "running" },
    ],
  },
};
const details = {
  activity_journal: [
    { id: "routing", sequence: 1, label: "Routed request", state: "completed" },
  ],
  orchestration: {
    activity_journal: [
      { id: "research", sequence: 1, label: "Compared evidence", state: "completed" },
    ],
  },
};

test("active activity prefers the live operation snapshot", () => {
  assert.equal(
    normaliseActivityJournal(operation, details, { active: true })[0].id,
    "live",
  );
});

test("completed activity prefers the rewritten orchestration journal", () => {
  assert.equal(
    normaliseActivityJournal(operation, details, { active: false })[0].id,
    "research",
  );
});

test("planned and skipped states remain truthful instead of becoming complete", () => {
  const entries = normaliseActivityJournal(
    null,
    {
      orchestration: {
        activity_journal: [
          { id: "one", sequence: 1, label: "Plan", state: "pending" },
          { id: "two", sequence: 2, label: "Read", state: "running" },
          { id: "three", sequence: 3, label: "Extra wave", state: "skipped" },
        ],
      },
    },
  );

  assert.deepEqual(entries.map((entry) => entry.state), ["pending", "running", "skipped"]);
  assert.equal(currentActivityEntry(entries).id, "two");
  assert.equal(activityEntryTelemetry(entries[0]), "Planned");
  assert.equal(activityEntryTelemetry(entries[2]), "Not needed");
});

test("activity telemetry omits missing numeric fields", () => {
  assert.equal(
    activityEntryTelemetry({ state: "running", token_count: null, character_count: null }),
    "In progress",
  );
  assert.equal(
    activityEntryTelemetry({ state: "running", token_count: 12, character_count: 84 }),
    "In progress · 12 tokens · 84 characters",
  );
});
