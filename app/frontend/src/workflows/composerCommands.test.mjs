import assert from "node:assert/strict";
import test from "node:test";

import {
  composerCommands,
  composerCommandSuggestions,
  readComposerCommand,
} from "./composerCommands.mjs";

test("a leading /image is image mode, and the request survives it", () => {
  assert.deepEqual(readComposerCommand("/image a red barn at sunrise"), {
    content: "a red barn at sunrise",
    modes: { image_mode: true },
  });
  assert.deepEqual(readComposerCommand("/photo a cow"), {
    content: "a cow",
    modes: { image_mode: true },
  });
  assert.deepEqual(
    readComposerCommand("  /research cheapest 2TB NVMe in Bangladesh"),
    {
      content: "cheapest 2TB NVMe in Bangladesh",
      modes: { research_mode: true },
    },
  );
});

test("an ordinary message is left alone", () => {
  for (const text of [
    "Generate an image of a cow.",
    "What does 20/20 vision mean?",
    "Use the / operator to divide.",
    "",
  ]) {
    assert.deepEqual(readComposerCommand(text), { content: text, modes: {} });
  }
});

test("a word that merely starts the same way is not the command", () => {

  const written = "/imagine a world without deadlines";
  assert.deepEqual(readComposerCommand(written), { content: written, modes: {} });
});

test("a command with nothing after it is not swallowed", () => {

  assert.deepEqual(readComposerCommand("/image"), {
    content: "/image",
    modes: {},
  });
  assert.deepEqual(readComposerCommand("/image   "), {
    content: "/image   ",
    modes: {},
  });
});

test("what it understands is published", () => {
  const commands = composerCommands();
  assert.deepEqual(
    commands.map((entry) => entry.command),
    ["/save mem", "/image", "/research"],
  );
  assert.ok(commands[1].aliases.includes("/photo"));
});

test("global memory is opt-in through /save mem", () => {
  assert.deepEqual(readComposerCommand("/save mem"), {
    content: "",
    modes: {},
    action: { type: "save_memory" },
  });
  assert.deepEqual(readComposerCommand("/save mem prefer concise replies"), {
    content: "prefer concise replies",
    modes: {},
    action: { type: "save_memory" },
  });
});

test("typing slash publishes and filters every command before submission", () => {
  assert.deepEqual(
    composerCommandSuggestions("/").map((entry) => entry.command),
    ["/save mem", "/image", "/research"],
  );
  assert.deepEqual(
    composerCommandSuggestions("/sa").map((entry) => entry.command),
    ["/save mem"],
  );
  assert.deepEqual(composerCommandSuggestions("/image a cat"), []);
  assert.deepEqual(composerCommandSuggestions("hello /"), []);
});
