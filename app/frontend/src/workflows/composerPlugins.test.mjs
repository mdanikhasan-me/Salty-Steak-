import assert from "node:assert/strict";
import test from "node:test";

import {
  composerPickerSections,
  connectionAction,
  connectionLabel,
} from "./composerPlugins.mjs";

const STATE = {
  tools: [
    {
      id: "web_search",
      name: "Web Research",
      description: "Reads public web pages.",
      availability: "available",
      category: "built_in_plugin",
    },
    {
      id: "images",
      name: "Vision",
      description: "Looks at an image.",
      availability: "model_required",
      unavailable_reason: "The vision runtime is not staged.",
      category: "attachment_plugin",
    },
  ],
  connected_apps: [
    {
      id: "mail.google",
      provider: "google",
      name: "Gmail",
      description: "Read and organise your mailbox.",
      account: "anik@example.com",
      state: "connected",
    },
    {
      id: "discord",
      provider: "discord",
      name: "Discord",
      description: "Servers and messages.",
      account: "",
      state: "not_connected",
    },
  ],
};

test("the picker keeps connected apps and memory management visible", () => {
  const sections = composerPickerSections(STATE);
  assert.deepEqual(
    sections.map((section) => section.id),
    ["connected_apps", "workspace"],
  );

  assert.equal(sections[0].title, "Connected apps");
  assert.deepEqual(
    sections[0].rows.map((row) => [row.name, row.state, row.action]),
    [
      ["Gmail", "Connected", "manage"],
      ["Discord", "Connect", "connect"],
    ],
  );

  assert.equal(sections[0].rows[0].detail, "anik@example.com");
});

test("runtime tools are routed dynamically rather than exposed as toggles", () => {
  const sections = composerPickerSections(STATE);
  assert.equal(sections.some((section) => section.id === "tools"), false);
  assert.equal(sections.flatMap((section) => section.rows).some((row) => row.id === "web_search"), false);
  const memory = sections.find((section) => section.id === "workspace").rows[0];
  assert.deepEqual([memory.id, memory.kind, memory.state], ["memory", "memory", "Manage"]);
});

test("a service that needs attention says so instead of claiming to be connected", () => {
  const app = {
    id: "mail.google",
    name: "Gmail",
    state: "connected",
    last_error: { message: "The token was revoked." },
  };
  assert.equal(connectionLabel(app), "Attention");
  assert.equal(connectionAction(app), "connect");
  assert.equal(connectionLabel({ state: "expired" }), "Reauthorize");
});

test("memory remains available even when no connector exists", () => {
  assert.deepEqual(
    composerPickerSections({ tools: [], connected_apps: [] }).map((section) => section.id),
    ["workspace"],
  );
  assert.deepEqual(composerPickerSections(null).map((section) => section.id), ["workspace"]);

  const legacy = composerPickerSections({
    plugins: [{ id: "terminal", name: "Terminal", availability: "available" }],
  });
  assert.deepEqual(legacy.map((section) => section.id), ["workspace"]);
});
