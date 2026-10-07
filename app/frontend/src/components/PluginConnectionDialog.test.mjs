import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("plugin connection setup is a real Streamable HTTP form with a fail-closed allowlist", async () => {
  const source = await readFile(new URL("./PluginConnectionDialog.jsx", import.meta.url), "utf8");
  assert.match(source, /Server endpoint/);
  assert.match(source, /Bearer token/);
  assert.match(source, /Allowed tools/);
  assert.match(source, /protects the bearer token for this Windows account/);
  assert.match(source, /if \(bearerToken\.trim\(\)\) request\.bearer_token/);
  assert.match(source, /discovered_tool_names/);
  assert.match(source, /Actions remain blocked until their tool names are explicitly allowed/);
  assert.doesNotMatch(source, /Calculator|Search or enter a web address/);
});
