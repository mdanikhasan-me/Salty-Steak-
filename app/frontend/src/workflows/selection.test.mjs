import assert from "node:assert/strict";
import test from "node:test";

import { sameSelectionId, selectedItemOrFirst } from "./selection.mjs";

test("selection IDs remain stable when the API changes number and string types", () => {
  assert.equal(sameSelectionId(27, "27"), true);
  assert.equal(sameSelectionId(null, "null"), false);
});

test("a filtered list replaces a stale selection with its first visible item", () => {
  const visible = [{ id: "matching-model" }, { id: "another-model" }];

  assert.equal(selectedItemOrFirst(visible, "hidden-model"), visible[0]);
  assert.equal(selectedItemOrFirst(visible, "another-model"), visible[1]);
  assert.equal(selectedItemOrFirst([], "hidden-model"), null);
});
