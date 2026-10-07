import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_TRAINING_PAGE,
  hashForPage,
  isTrainingPage,
  pageFromHash,
  validTrainingPage,
} from "./navigation.mjs";

test("the hash is the authoritative page token", () => {
  assert.equal(pageFromHash("#evaluate?version=abc"), "evaluate");
  assert.equal(pageFromHash("#chat"), "chat");
  assert.equal(pageFromHash("#about?from=chat"), "about");
  assert.equal(pageFromHash("#not-a-page"), "chat");
});

test("training center membership is explicit", () => {
  assert.equal(isTrainingPage("data"), true);
  assert.equal(isTrainingPage("versions"), true);
  assert.equal(isTrainingPage("evaluate"), true);
  assert.equal(validTrainingPage("evaluate"), "evaluate");
  assert.equal(isTrainingPage("chat"), false);
  assert.equal(isTrainingPage("project"), false);
  assert.equal(isTrainingPage("about"), false);
});

test("an invalid remembered training tab returns to the first workflow step", () => {
  assert.equal(validTrainingPage("train"), "train");
  assert.equal(validTrainingPage("chat"), DEFAULT_TRAINING_PAGE);
  assert.equal(validTrainingPage(null), DEFAULT_TRAINING_PAGE);
});

test("navigation preserves route parameters without creating another page state", () => {
  assert.equal(hashForPage("train", { version: "saved 2" }), "#train?version=saved+2");
  assert.equal(hashForPage("unknown"), "#chat");
});
