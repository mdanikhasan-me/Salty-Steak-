import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("Turtle persists independently from effort and reaches every request", () => {
  const snapshot = normaliseGenerationSettingsSnapshot({reasoning_mode:"cooking",resource_mode:"turtle"});
  assert.equal(snapshot.resource_mode,"turtle");
  assert.equal(snapshot.reasoning_mode,"cooking");
  assert.equal(generationSettingsForRequest(snapshot).resource_mode,"turtle");
  assert.equal(generationTurnSettingsForRequest(snapshot,{}, {researchMode:true}).resource_mode,"turtle");
  assert.equal(normaliseGenerationSettingsSnapshot({resource_mode:"turbo"}).resource_mode,"normal");
});

import {
  COOKING_MODES,
  COMPUTER_AUTHORITY_MODES,
  CONTEXT_WINDOW_PRESETS,
  MAXIMUM_OUTPUT_MODES,
  MAXIMUM_OUTPUT_TOKEN_PRESETS,
  contextWindowForMaximumOutput,
  cookingModeLabel,
  computerAuthorityLabel,
  nextEnabledMenuIndex,
  generationSettingsForRequest,
  generationTurnSettingsForRequest,
  normaliseGenerationSettingsSnapshot,
  normaliseCookingMode,
  normaliseComputerAuthorityMode,
  normaliseContextWindowTokens,
  normaliseMaximumOutputMode,
  normaliseMaximumOutputTokens,
  toggleComposerMenu,
} from "./composerControls.mjs";

test("composer menus are mutually exclusive and toggle closed", () => {
  assert.equal(toggleComposerMenu(null, "attachments"), "attachments");
  assert.equal(toggleComposerMenu("attachments", "plugins"), "plugins");
  assert.equal(toggleComposerMenu("plugins", "plugins"), null);
  assert.equal(toggleComposerMenu("plugins", "unknown"), null);
});

test("cooking exposes exactly Instant and Cooking with safe legacy migration", () => {
  assert.deepEqual(COOKING_MODES.map((mode) => mode.id), ["instant", "cooking"]);
  assert.deepEqual(COOKING_MODES.map((mode) => mode.label), ["Instant", "Cooking"]);
  assert.equal(normaliseCookingMode("COOKING"), "cooking");
  assert.equal(normaliseCookingMode("off"), "instant");
  assert.equal(normaliseCookingMode("auto"), "cooking");
  assert.equal(normaliseCookingMode("deep"), "cooking");
  assert.equal(normaliseCookingMode("unsupported"), "instant");
  assert.equal(cookingModeLabel("instant"), "Instant");
  assert.equal(cookingModeLabel("cooking"), "Cooking");
});

test("computer authority is explicit, persisted, and defaults to Ask every time", () => {
  assert.deepEqual(
    COMPUTER_AUTHORITY_MODES.map((mode) => mode.id),
    ["ask_every_time", "full_access"],
  );
  assert.equal(normaliseComputerAuthorityMode("full_access"), "full_access");
  assert.equal(normaliseComputerAuthorityMode("anything else"), "ask_every_time");
  assert.equal(computerAuthorityLabel("ask_every_time"), "Ask");
  assert.equal(computerAuthorityLabel("full_access"), "Full access");
});

test("generation presets are exact and bounded to the public contract", () => {
  assert.deepEqual(
    CONTEXT_WINDOW_PRESETS.map((preset) => [preset.label, preset.tokens]),
    [
      ["16K", 16_384],
      ["24K", 24_576],
      ["32K", 32_768],
      ["48K", 49_152],
      ["64K", 65_536],
      ["96K", 98_304],
      ["128K", 131_072],
      ["192K", 196_608],
      ["262K", 262_144],
    ],
  );
  assert.deepEqual(MAXIMUM_OUTPUT_MODES.map((mode) => mode.id), ["automatic", "manual"]);
  assert.deepEqual(
    MAXIMUM_OUTPUT_TOKEN_PRESETS,
    [256, 512, 1_024, 2_048, 4_096, 8_192, 16_384, 32_768],
  );
  assert.equal(normaliseContextWindowTokens(65_536), 65_536);
  assert.equal(normaliseContextWindowTokens(262_144), 262_144);
  assert.equal(normaliseContextWindowTokens(8_192), 32_768);
  assert.equal(normaliseMaximumOutputMode("MANUAL"), "manual");
  assert.equal(normaliseMaximumOutputMode("invalid"), "automatic");
  assert.equal(normaliseMaximumOutputTokens(4_096), 4_096);
  assert.equal(normaliseMaximumOutputTokens(32_768), 32_768);
  assert.equal(normaliseMaximumOutputTokens(123), 512);
  assert.equal(contextWindowForMaximumOutput(8_192, 32_768), 32_768);
  assert.equal(contextWindowForMaximumOutput(32_768, 32_768), 65_536);
  assert.equal(contextWindowForMaximumOutput(16_384, 16_384), 32_768);
});

test("keyboard navigation wraps while skipping unavailable menu items", () => {
  const enabled = [true, false, true];
  assert.equal(nextEnabledMenuIndex(0, "next", enabled), 2);
  assert.equal(nextEnabledMenuIndex(2, "next", enabled), 0);
  assert.equal(nextEnabledMenuIndex(0, "previous", enabled), 2);
  assert.equal(nextEnabledMenuIndex(2, "first", enabled), 0);
  assert.equal(nextEnabledMenuIndex(0, "last", enabled), 2);
  assert.equal(nextEnabledMenuIndex(0, "next", [false, false]), -1);
});

test("remembered Cooking state is normalized and retained in the request contract", () => {
  const defaults = {
    context_window_tokens: 32_768,
    maximum_output_mode: "automatic",
    maximum_output_tokens: 8_192,
    reasoning_mode: "instant",
    system_prompt: "",
    stop_sequences: [],
    computer_authority_mode: "ask_every_time",
  };
  const remembered = normaliseGenerationSettingsSnapshot({
    ...defaults,
    reasoning_mode: "COOKING",
    maximum_output_mode: "MANUAL",
    maximum_output_tokens: 2_048,
    stop_sequences: ["END"],
    ignored_property: "not sent",
  }, defaults);
  const request = generationSettingsForRequest(remembered, defaults);

  assert.equal(request.reasoning_mode, "cooking");
  assert.equal(request.maximum_output_mode, "manual");
  assert.equal(request.maximum_output_tokens, 2_048);
  assert.deepEqual(request.stop_sequences, ["END"]);
  assert.equal(request.computer_authority_mode, "ask_every_time");
  assert.equal(Object.hasOwn(request, "ignored_property"), false);
  assert.notEqual(request.stop_sequences, remembered.stop_sequences);
});

test("each composer turn sends explicit capability booleans without choosing the learned route", () => {
  const defaults = {
    context_window_tokens: 32_768,
    maximum_output_mode: "automatic",
    maximum_output_tokens: 8_192,
    reasoning_mode: "instant",
    web_search_enabled: false,
    system_prompt: "",
    stop_sequences: [],
    computer_authority_mode: "ask_every_time",
  };
  const ordinary = generationTurnSettingsForRequest(defaults, defaults);
  assert.equal(ordinary.agent_mode, false);
  assert.equal(ordinary.research_available, false);
  assert.equal(ordinary.research_command, false);
  assert.equal(ordinary.image_mode, false);
  assert.equal(ordinary.web_search_enabled, false);

  const available = generationTurnSettingsForRequest(defaults, defaults, {
    agentMode: true,
    researchMode: true,
  });
  assert.equal(available.agent_mode, true);
  assert.equal(available.research_available, true);
  assert.equal(available.research_command, true);
  assert.equal(available.web_search_enabled, true);

  const commanded = generationTurnSettingsForRequest(defaults, defaults, {
    researchCommand: true,
    imageCommand: true,
  });
  assert.equal(commanded.research_available, true);
  assert.equal(commanded.research_command, true);
  assert.equal(commanded.image_mode, true);
  assert.equal(commanded.web_search_enabled, true);
});

test("the Chat composer wires attachments, Plugins, model settings, and Cooking distinctly", async () => {
  const source = await readFile(new URL("../pages/ChatPage.jsx", import.meta.url), "utf8");
  const responseControl = await readFile(new URL("../components/ResponseModeControl.jsx", import.meta.url), "utf8");
  const menuIds = [
    "composer-authority-menu",
    "composer-cooking-menu",
  ];

  for (const menuId of menuIds) {
    assert.match(source, new RegExp(`aria-controls=\\"${menuId}\\"`));
    assert.match(source + responseControl, new RegExp(`id=\\"${menuId}\\"`));
  }
  assert.match(source, /aria-label="Add files"/);
  assert.match(source, /onClick=\{\(\) => attachmentInputRef\.current\?\.click\(\)\}/);
  assert.doesNotMatch(source, /id="composer-attachments-menu"/);
  assert.doesNotMatch(source, /label="Add photo"/);
  assert.match(source, /reasoning_mode:mode/);
  assert.match(source, /context_window_tokens: 32_768/);
  assert.match(source, /maximum_output_mode: "automatic"/);
  assert.match(source, /maximum_output_tokens: 32_768/);
  assert.match(source, /reasoning_mode: "instant"/);
  assert.match(source, /computer_authority_mode: "ask_every_time"/);
  assert.match(source, /Computer authority/);
  assert.match(source, /COMPUTER_AUTHORITY_MODES\.map/);
  assert.match(source, /computerAuthorityLabel/);
  assert.match(source, /chatStatus\.generation_defaults/);
  assert.match(source, /salty-steak:generation-settings-v2/);



  assert.match(source, /aria-label="Tools and connected apps"/);
  assert.match(source, /openSettings\("plugins"\)/);
  assert.match(source, /openSettings\("response"\)/);
  assert.doesNotMatch(source, /openSettings\("tools"/);
  assert.doesNotMatch(source, /runCalculator|browserAddress/);
  assert.doesNotMatch(source, /<span>Attach<\/span>/);
  assert.doesNotMatch(source, /<span>Standard<\/span>/);
});
