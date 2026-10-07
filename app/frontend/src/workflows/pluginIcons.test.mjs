import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";





const source = readFileSync(
  new URL("../pages/ChatPage.jsx", import.meta.url),
  "utf8",
);

const REGISTERED = [
  "web_search",
  "text_files",
  "images",
  "terminal",
  "screen_capture",
  "screen_recording",
  "app_control",
];

function iconMap() {
  const start = source.indexOf("const PLUGIN_ICONS = {");
  const end = source.indexOf("const PLUGIN_CATEGORY_ICONS");
  assert.ok(start !== -1 && end > start, "the icon map should exist");
  return source.slice(start, end);
}

test("every registered plugin has its own icon", () => {
  const block = iconMap();
  const entries = new Map(
    [...block.matchAll(/(\w+):\s*(\w+),/g)].map((match) => [match[1], match[2]]),
  );
  for (const id of REGISTERED) {
    assert.ok(entries.has(id), `${id} has no icon`);
  }

  const icons = new Set(REGISTERED.map((id) => entries.get(id)));
  assert.equal(icons.size, REGISTERED.length);
});

test("an unknown plugin still falls back rather than rendering nothing", () => {
  assert.match(source, /\|\|\s*Plug\s*\n?\s*\);/);
});
