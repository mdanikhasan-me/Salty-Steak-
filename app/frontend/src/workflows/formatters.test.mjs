import assert from "node:assert/strict";
import test from "node:test";

import {
  formatDate,
  formatDuration,
  formatNumber,
  versionLabel,
} from "./formatters.js";

test("unknown numeric values are not presented as zero", () => {
  assert.equal(formatNumber(null), "Not reported");
  assert.equal(formatNumber(undefined), "Not reported");
  assert.equal(formatNumber(""), "Not reported");
  assert.equal(formatNumber(0), "0");
});

test("checkpoint labels never invent an unknown cumulative step count", () => {
  assert.equal(
    versionLabel({ total_trained_steps: null, imported_number: 2 }),
    "Salty Steak imported version 2",
  );
  assert.equal(
    versionLabel({ total_trained_steps: 540 }),
    "Salty Steak at 540 total steps",
  );
});

test("timestamps require the backend ISO contract", () => {
  assert.notEqual(formatDate("2026-07-25T12:00:00Z"), "Unavailable");
  assert.equal(formatDate(1_784_000_000), "Unavailable");
  assert.equal(formatDate(null), "Unavailable");
});

test("short measured durations retain useful subsecond precision", () => {
  assert.equal(formatDuration(0), "0 sec");
  assert.equal(formatDuration(0.8), "0.8 sec");
  assert.equal(formatDuration(4.1), "4.1 sec");
  assert.equal(formatDuration(12.6), "13 sec");
});
