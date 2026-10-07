import assert from "node:assert/strict";
import test from "node:test";

import {
  activityAccount,
  activityPhases,
  outcomeLabel,
} from "./activitySummary.mjs";



const RESEARCH_RUN = [
  { action: "browser.control", arguments: { command: "open_url" }, status: "succeeded", duration_ms: 3610 },
  { action: "browser.control", arguments: { command: "query" }, status: "succeeded", duration_ms: 16 },
  { action: "browser.control", arguments: { command: "set_value" }, status: "succeeded", duration_ms: 391 },
  { action: "browser.control", arguments: { command: "submit" }, status: "failed", duration_ms: 120 },
  { action: "browser.control", arguments: { command: "read_page" }, status: "succeeded", duration_ms: 2100 },
  { action: "browser.control", arguments: { command: "query" }, status: "succeeded", duration_ms: 22 },
];

test("eight browser calls to answer one question is one act of searching", () => {
  const phases = activityPhases(RESEARCH_RUN, { sites: 4, pages: 6 });
  assert.equal(phases.length, 1);


  assert.equal(phases[0].label, "Searched 4 websites");
  assert.equal(phases[0].detail, "6 pages");
  assert.equal(phases[0].steps, 6);
  assert.equal(phases[0].failed, 1);
  assert.equal(phases[0].milliseconds, 6259);

  assert.equal(phases[0].events.length, 6);
});

test("a run with no pages counts what it actually did", () => {
  const phases = activityPhases(RESEARCH_RUN);
  assert.equal(phases[0].detail, "6 pages");
});

test("phases follow the work, in the order the work happened", () => {
  const phases = activityPhases([
    { action: "browser.control", status: "succeeded" },
    { action: "application.launch", status: "succeeded" },
    { action: "window.control", status: "succeeded" },
    { action: "screen.capture", status: "succeeded" },
    { action: "browser.control", status: "succeeded" },
  ]);
  assert.deepEqual(
    phases.map((phase) => phase.label),
    ["Searched the web", "Used this computer", "Looked at the screen", "Searched the web"],
  );


  assert.equal(phases[1].steps, 2);
});

test("a connected app is named, not its capability string", () => {
  const phases = activityPhases([
    { action: "gmail.search", status: "succeeded" },
    { action: "gmail.apply_label", status: "succeeded" },
  ]);
  assert.equal(phases[0].label, "Used Gmail");
  assert.equal(phases[0].detail, "2 operations");
});

test("partial is an engineering verdict, not something to show a reader", () => {


  assert.equal(outcomeLabel("partial"), "Incomplete");
  assert.equal(outcomeLabel("failed"), "Couldn't finish");
  assert.equal(outcomeLabel("stopped"), "Stopped");
  assert.equal(outcomeLabel("waiting"), "Waiting for you");

  assert.equal(outcomeLabel("done"), "");
  assert.equal(outcomeLabel("cooked"), "");
});

test("a turn that executed nothing has nothing to disclose", () => {
  const account = activityAccount([], { state: "done" });
  assert.deepEqual(account.phases, []);
  assert.equal(account.expandable, false);
  assert.equal(account.steps, 0);
});

test("the account carries both the summary and the outcome", () => {
  const account = activityAccount(RESEARCH_RUN, {
    sites: 4,
    pages: 6,
    state: "partial",
  });
  assert.equal(account.phases.length, 1);
  assert.equal(account.steps, 6);
  assert.equal(account.expandable, true);
  assert.equal(account.outcome, "Incomplete");
});
