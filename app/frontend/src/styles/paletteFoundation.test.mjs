import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

function relativeLuminance(hex) {
  const channels = hex.match(/[0-9a-f]{2}/gi).map((channel) => Number.parseInt(channel, 16) / 255);
  const [red, green, blue] = channels.map((channel) => (
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
  ));
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function contrast(left, right) {
  const first = relativeLuminance(left);
  const second = relativeLuminance(right);
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
}

test("foundation uses one low-chroma warm graphite palette", async () => {
  const tokens = await readFile(new URL("./tokens-base.css", import.meta.url), "utf8");

  for (const declaration of [
    "--chrome: #141413",
    "--rail: #191918",
    "--canvas: #1d1d1c",
    "--surface: #242423",
    "--popover: #2a2a28",
    "--accent: #b07c63",
  ]) {
    assert.match(tokens, new RegExp(declaration));
  }
  assert.doesNotMatch(tokens, /#ef8f5b|#f59d6c|#dd7f4c/i);
});

test("primary text, secondary text, and copper actions meet contrast gates", () => {
  assert.ok(contrast("f2f0ed", "1d1d1c") >= 7);
  assert.ok(contrast("a19b95", "1d1d1c") >= 4.5);
  assert.ok(contrast("b07c63", "171513") >= 4.5);
});

test("late palette bridge removes the composer gradient and aligns overlay surfaces", async () => {
  const global = await readFile(new URL("./global.css", import.meta.url), "utf8");
  const r18 = await readFile(new URL("./design-r18.css", import.meta.url), "utf8");

  assert.match(global, /\.composer\s*\{\s*background: var\(--canvas\)/);
  assert.match(global, /\.generation-inspector\s*\{\s*background: var\(--surface\)/);
  assert.match(global, /\.cooking-activity\s*\{\s*background: var\(--rail\)/);
  assert.match(r18, /--r18-accent: var\(--accent\)/);
  assert.doesNotMatch(r18.slice(0, r18.indexOf("/* Chat settings")), /rgb\(239 143 91/);
});

test("native chrome and modal interaction use the same calm foundation", async () => {
  const [nativeHost, startup, r18] = await Promise.all([
    readFile(new URL("../../../desktop/native/Program.cs", import.meta.url), "utf8"),
    readFile(new URL("../../../desktop/native/StartupWindowV2.cs", import.meta.url), "utf8"),
    readFile(new URL("./design-r18.css", import.meta.url), "utf8"),
  ]);

  assert.match(nativeHost, /captionColor = 0x00131414;/);
  assert.match(nativeHost, /BackColor = Color\.FromArgb\(29, 29, 28\)/);
  assert.match(startup, /Surface = Color\.FromArgb\(20, 21, 22\)/);
  assert.match(r18, /\.chat-settings-layer\s*\{[^}]*inset:\s*0;/s);
  assert.match(r18, /\.composer-selector--cooking\s*\{\s*color:\s*var\(--r18-muted\)/);
});
