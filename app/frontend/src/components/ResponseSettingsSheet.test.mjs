import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const componentUrl = new URL("./ResponseSettingsSheet.jsx", import.meta.url);
const stylesUrl = new URL("../styles/response-settings-sheet.css", import.meta.url);
const chatPageUrl = new URL("../pages/ChatPage.jsx", import.meta.url);
const chatStylesUrl = new URL("../styles/chat.css", import.meta.url);

test("response settings keep model, quick limits, and every generation field together", async () => {
  const source = await readFile(componentUrl, "utf8");

  assert.match(source, /Use model/);
  assert.match(source, /Context window/);
  assert.match(source, /Max tokens/);
  assert.doesNotMatch(source, /Response profile/);
  assert.match(source, /CONTEXT_WINDOW_PRESETS\.map/);
  assert.match(source, /MAXIMUM_OUTPUT_MODES\.map/);
  assert.match(source, /MAXIMUM_OUTPUT_TOKEN_PRESETS\.map/);
  assert.doesNotMatch(source, /model\.ready === false \? " — setup required"/);
  for (const key of [
    "temperature",
    "top_p",
    "top_k",
    "repetition_penalty",
    "seed",
    "system_prompt",
    "reasoning_visibility",
    "image_model_id",
    "image_aspect_ratio",
    "image_resolution",
    "image_steps",
  ]) assert.match(source, new RegExp(key));
  assert.match(source, /Raw local trace/);
  assert.match(source, /type="checkbox"/);
  assert.doesNotMatch(source, />Cooking</);
});

test("response settings use preset selects and one progressive disclosure, not slider stacks", async () => {
  const [source, styles] = await Promise.all([
    readFile(componentUrl, "utf8"),
    readFile(stylesUrl, "utf8"),
  ]);

  assert.match(source, /aria-label="Context window"/);
  assert.match(source, /aria-label="Max tokens mode"/);
  assert.match(source, /aria-label="Manual max tokens"/);
  assert.match(source, /aria-label="Image model"/);
  assert.match(source, /aria-label="Image aspect ratio"/);
  assert.match(source, /aria-label="Image resolution"/);
  assert.match(source, /aria-label="Image quality"/);
  assert.match(source, /maximum_output_mode === "manual"/);
  assert.match(source, /type="number"/);
  assert.doesNotMatch(source, /type="range"/);
  assert.match(source, /<details className="response-settings-sheet__advanced" open=\{initialSection === "advanced" \|\| undefined\}>/);
  assert.match(styles, /\.response-settings-sheet__quick-grid/);
  assert.match(styles, /\.response-settings-sheet__output-controls/);
  assert.match(styles, /width:\s*min\(520px,/);
  assert.match(styles, /grid-template-columns:\s*minmax\(102px,\s*\.95fr\)\s*minmax\(112px,\s*1\.05fr\)/);
  assert.match(styles, /@media \(max-width: 520px\)/);
  assert.doesNotMatch(styles, /border-radius:\s*999/);
});

test("a registered model remains named before its runtime is activated", async () => {
  const [source, chatStyles] = await Promise.all([
    readFile(chatPageUrl, "utf8"),
    readFile(chatStylesUrl, "utf8"),
  ]);

  assert.match(source, /chatStatus\?\.active_target_id\s*\|\|\s*chatStatus\?\.selected_model_id/);
  assert.doesNotMatch(chatStyles, /composer-selector:first-of-type\s*\{[^}]*max-width:\s*120px/s);
});

test("image settings are directly reachable from the Chat composer", async () => {
  const [sheet, chat] = await Promise.all([
    readFile(componentUrl, "utf8"),
    readFile(chatPageUrl, "utf8"),
  ]);

  assert.match(chat, /aria-label={`Image settings:/);
  assert.ok(chat.includes('onClick={() => openSettings("image")}'));
  assert.match(chat, /initialSection={settingsView}/);
  assert.match(sheet, /initialSection = "response"/);
  assert.match(sheet, /imageSectionRef\.current\?\.scrollIntoView/);
  assert.doesNotMatch(sheet, /imageSectionRef\.current\?\.querySelector\("select"\)\?\.focus/);
});
