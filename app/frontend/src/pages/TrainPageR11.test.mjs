import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("normal training never auto-activates without the scientific gates", async () => {
  const source = await readFile(new URL("./TrainPageR11.jsx", import.meta.url), "utf8");

  assert.match(source, /const POST_TRAINING_POLICY = "evaluate";/);
  assert.doesNotMatch(source, /const POST_TRAINING_POLICY = "evaluate_and_activate";/);
  assert.match(source, /result is saved but not activated/i);
  assert.match(source, /English-retention[\s\S]*generation acceptance suite/);
});

test("learned identity post-training is exposed as a gated native workflow", async () => {
  const source = await readFile(new URL("./TrainPageR11.jsx", import.meta.url), "utf8");

  assert.match(source, /Base Steak 2\.0 identity post-training/);
  assert.match(source, /one rank-512 GGUF identity extension/);
  assert.match(source, /No system-prompt[\s\S]*hardcoded answer is used/);
  assert.match(source, /api\.startIdentityPostTraining/);
  assert.match(source, /byte-equivalent capability[\s\S]*retention to pass/);
  assert.match(source, /routing-only learned adapter selects the[\s\S]*identity lane/);
  assert.match(source, /activation.*Model-routed/s);
  assert.match(source, /rank-32 routing-only adapter/);
  assert.match(source, /response, research, image,[\s\S]*agent,[\s\S]*identity work/);
  assert.match(source, /Routing holdout/);
  assert.match(source, /<Disclosure summary="Identity post-training" open=\{identityActive\}/);
  assert.doesNotMatch(source, /if \(!readyDatasets\.length \|\| !eligibleVersions\.length\) \{\s*return/);
});
