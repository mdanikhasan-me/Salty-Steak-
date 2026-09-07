import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("Plugins panel exposes the requested integrations without legacy tools", async () => {
  const source = await readFile(new URL("./PluginsPanel.jsx", import.meta.url), "utf8");
  const definitions = await readFile(new URL("../workflows/pluginConnections.mjs", import.meta.url), "utf8");
  const computerControl = await readFile(new URL("./ComputerControlPermissions.jsx", import.meta.url), "utf8");
  const implementation = `${source}\n${definitions}\n${computerControl}`;

  for (const label of [
    "Gmail",
    "Google Calendar",
    "iCloud Calendar",
    "MCP server",
    "Add server",
    "Computer access",
  ]) {
    assert.match(implementation, new RegExp(label));
  }
  assert.doesNotMatch(implementation, /Calculator|manual URL|browserAddress|type="url"/i);
});

test("Plugins panel keeps external connectivity truthful and callback-driven", async () => {
  const source = await readFile(new URL("./PluginsPanel.jsx", import.meta.url), "utf8");

  assert.match(source, /connections = \{\}/);
  assert.doesNotMatch(source, /Automatic web search/);
  assert.match(source, /disabled=\{presentation\.disabled \|\| !handlerAvailable\}/);
  assert.match(source, /mcpPresentation\.stateLabel/);
  assert.match(source, /Connections could not be loaded/);
  assert.match(source, /onConnect/);
  assert.match(source, /onManage/);
  assert.match(source, /onAddMcpServer/);
  assert.match(source, /onRetry/);
  assert.match(source, /<ComputerControlPermissions/);
  assert.match(source, /adapter=\{automationAdapter\}/);
});

test("Chat wires the real automation adapter instead of plugin capability metadata", async () => {
  const page = await readFile(new URL("../pages/ChatPage.jsx", import.meta.url), "utf8");

  assert.match(page, /const AUTOMATION_ADAPTER = createComputerControlAdapter/);
  assert.match(page, /automationAdapter=\{AUTOMATION_ADAPTER\}/);
  assert.doesNotMatch(page, /capabilities=\{pluginState\?\.plugins/);
});

test("computer automation uses the shared active phrase without blinking", async () => {
  const automation = await readFile(new URL("./ComputerControlPermissions.jsx", import.meta.url), "utf8");
  assert.match(automation, /aria-busy=\{busy \|\| undefined\}/);
  assert.match(automation, /activity-phrase activity-phrase--compact/);
  assert.match(automation, /aria-live="polite"/);
  assert.match(automation, /aria-atomic="true"/);
});

test("Plugins styling is a divider list without per-row cards or decoration", async () => {
  const css = await readFile(new URL("../styles/plugins-panel.css", import.meta.url), "utf8");

  assert.match(css, /\.plugin-row\s*\{[\s\S]*border-bottom:/);
  assert.doesNotMatch(css, /\.plugin-row\s*\{[^}]*background:/);
  assert.doesNotMatch(css, /linear-gradient|radial-gradient|box-shadow/i);
});
