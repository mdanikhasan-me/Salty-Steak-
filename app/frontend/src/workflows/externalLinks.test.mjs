import assert from "node:assert/strict";
import test from "node:test";

import { isExternalAddress, externalLinkFromEvent } from "./externalLinks.mjs";

test("only web addresses are treated as external", () => {
  assert.equal(isExternalAddress("https://example.com/a"), true);
  assert.equal(isExternalAddress("http://example.com"), true);
  assert.equal(isExternalAddress("#anchor"), false);
  assert.equal(isExternalAddress("/local/path"), false);
  assert.equal(isExternalAddress("file:///C:/secret.txt"), false);
  assert.equal(isExternalAddress("javascript:alert(1)"), false);
  assert.equal(isExternalAddress(""), false);
});

function clickOn(href, overrides = {}) {
  return {
    button: 0,
    defaultPrevented: false,
    target: { closest: () => (href ? { getAttribute: () => href } : null) },
    ...overrides,
  };
}

test("a plain left click on a web link is taken", () => {
  assert.equal(
    externalLinkFromEvent(clickOn("https://example.com/x")),
    "https://example.com/x",
  );
});

test("a modified click is left to the platform", () => {
  assert.equal(externalLinkFromEvent(clickOn("https://x.test", { ctrlKey: true })), "");
  assert.equal(externalLinkFromEvent(clickOn("https://x.test", { metaKey: true })), "");
  assert.equal(externalLinkFromEvent(clickOn("https://x.test", { button: 1 })), "");
});

test("a click that is not on a link, or already handled, is ignored", () => {
  assert.equal(externalLinkFromEvent(clickOn("")), "");
  assert.equal(
    externalLinkFromEvent(clickOn("https://x.test", { defaultPrevented: true })),
    "",
  );
});
