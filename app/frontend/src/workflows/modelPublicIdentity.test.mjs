import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const versionsPage = new URL("../pages/VersionsPageR11.jsx", import.meta.url);

test("the model catalogue keeps private engine codenames out of product copy", async () => {
  const source = await readFile(versionsPage, "utf8");

  assert.doesNotMatch(source, /Steak35/);
  assert.match(source, /Private Salty native engine/);
  assert.match(source, /public_architecture_name/);
});
