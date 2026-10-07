import test from "node:test";
import assert from "node:assert/strict";
import { createFreshImageSeed, generatedImageForMessage } from "./generatedImages.mjs";

const valid = {
  role: "assistant",
  technical_details: {
    generated_image: {
      schema: "salty-steak-generated-image-v1",
      id: "image_12345678",
      sha256: "a".repeat(64),
      media_type: "image/png",
      width: 512,
      height: 512,
      size_bytes: 1234,
      prompt: "A quiet room",
      model_name: "Steak gen 1 ScaledFP8",
    },
  },
};

test("a validated generated image becomes a same-origin artifact URL", () => {
  assert.deepEqual(generatedImageForMessage(valid), {
    id: "image_12345678",
    sha256: "a".repeat(64),
    mediaType: "image/png",
    width: 512,
    height: 512,
    sizeBytes: 1234,
    prompt: "A quiet room",
    modelName: "Steak gen 1 ScaledFP8",
    url: "/api/chat/image-artifacts/image_12345678",
  });
});

test("untrusted paths and malformed image metadata fail closed", () => {
  assert.equal(generatedImageForMessage({ ...valid, role: "user" }), null);
  for (const generated_image of [
    { ...valid.technical_details.generated_image, id: "../../secret" },
    { ...valid.technical_details.generated_image, sha256: "bad" },
    { ...valid.technical_details.generated_image, media_type: "text/html" },
    { ...valid.technical_details.generated_image, width: 99999 },
  ]) {
    assert.equal(generatedImageForMessage({
      role: "assistant",
      technical_details: { generated_image },
    }), null);
  }
});

test("fresh image seeds use all 53 safe integer bits and remain reproducible when reused", () => {
  const seed = createFreshImageSeed(new Uint32Array([0x12345678, 0x9abcdef0]));
  assert.equal(seed, createFreshImageSeed(new Uint32Array([0x12345678, 0x9abcdef0])));
  assert.notEqual(seed, createFreshImageSeed(new Uint32Array([0x12345678, 0x9abcdeef])));
  assert.equal(Number.isSafeInteger(seed), true);
  assert.ok(seed >= 0);
});
