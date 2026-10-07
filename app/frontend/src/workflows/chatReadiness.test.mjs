import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const chatPage = readFileSync(
  new URL("../pages/ChatPage.jsx", import.meta.url),
  "utf8",
);

test("preparing chat status is polled until the runtime becomes ready", () => {
  assert.match(chatPage, /readiness\.key !== "preparing"/);
  assert.match(chatPage, /window\.setInterval/);
  assert.match(chatPage, /refreshDomain\("chat", \{ quiet: true \}\)/);
  assert.match(chatPage, /window\.clearInterval/);
});
