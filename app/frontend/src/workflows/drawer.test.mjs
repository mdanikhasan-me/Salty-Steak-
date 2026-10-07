import assert from "node:assert/strict";
import test from "node:test";

import { drawerReducer } from "./drawer.mjs";

test("the menu control opens and toggles the navigation drawer", () => {
  assert.equal(drawerReducer(false, { type: "open" }), true);
  assert.equal(drawerReducer(false, { type: "toggle" }), true);
  assert.equal(drawerReducer(true, { type: "toggle" }), false);
});

test("navigation preserves the desktop sidebar preference", () => {
  assert.equal(drawerReducer(true, { type: "navigate" }), true);
  assert.equal(drawerReducer(true, { type: "page_changed" }), true);
  assert.equal(drawerReducer(false, { type: "page_changed" }), false);
  assert.equal(drawerReducer(true, { type: "close" }), false);
});
