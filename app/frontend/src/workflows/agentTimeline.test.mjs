import assert from "node:assert/strict";
import test from "node:test";

import {
  eventsFromPlanNodes,
  eventArtifact,
  eventSummary,
  formatDuration,
} from "./agentTimeline.mjs";

test("a plan node carries its measured duration and its artifact", () => {
  const events = eventsFromPlanNodes([
    {
      node: "shot",
      capability: "screen.capture",
      state: "completed",
      duration_ms: 412.5,
      observation: {
        artifact: { path: "C:/artifacts/screen.bmp", width: 1920, height: 1080 },
      },
    },
  ]);

  assert.equal(events.length, 1);
  assert.equal(events[0].action, "screen.capture");
  assert.equal(formatDuration(events[0].duration_ms), "413ms");
  assert.deepEqual(eventArtifact(events[0]), {
    path: "C:/artifacts/screen.bmp",
    width: 1920,
    height: 1080,
  });
  assert.equal(eventSummary(events[0]), "Capture the current display");
});

test("a failed node keeps its error alongside anything it produced", () => {
  const [event] = eventsFromPlanNodes([
    {
      node: "open",
      capability: "application.launch",
      state: "failed",
      failure: "ERROR_FILE_NOT_FOUND",
    },
  ]);

  assert.equal(event.status, "failed");
  assert.equal(event.observation.error, "ERROR_FILE_NOT_FOUND");
});

test("a node naming nothing runnable produces no block", () => {
  assert.deepEqual(eventsFromPlanNodes([{ node: "a" }, null]), []);
});
