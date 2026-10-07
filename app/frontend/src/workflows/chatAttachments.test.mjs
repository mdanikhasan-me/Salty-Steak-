import assert from "node:assert/strict";
import test from "node:test";

import {
  attachmentKind,
  attachmentPromptSuffix,
  safeAttachmentName,
  validateAttachment,
  validateVisionAttachment,
} from "./chatAttachments.mjs";

test("images use vision while every other file reaches backend inspection", () => {
  assert.equal(attachmentKind({ name: "photo.png", type: "image/png" }), "image");
  assert.equal(attachmentKind({ name: "notes.md", type: "" }), "file");
  assert.equal(attachmentKind({ name: "archive.zip", type: "application/zip" }), "file");
  assert.equal(validateAttachment({ name: "photo.png", type: "image/png", size: 12 }).code, "vision_required");
  assert.equal(validateAttachment({ name: "notes.md", type: "", size: 12 }).accepted, true);
});

test("attachment limits are enforced cumulatively before reading content", () => {
  assert.equal(validateAttachment({ name: "big.bin", type: "", size: 512 * 1024 * 1024 + 1 }).code, "file_too_large");
  assert.equal(validateAttachment({ name: "next.zip", type: "", size: 20 }, 2 * 1024 * 1024 * 1024 - 10).code, "message_too_large");
  assert.equal(validateAttachment({ name: "extra.txt", size: 1 }, 0, 32).code, "too_many_files");
});

test("vision attachments are status gated, exact-type, up to twelve, and 25 MiB", () => {
  const ready = { application_available: true };
  const file = { name: "photo.png", type: "image/png", size: 1024 };
  assert.equal(validateVisionAttachment(file, ready).accepted, true);
  assert.equal(validateVisionAttachment(file, ready, 1, 1).accepted, true);
  assert.equal(validateVisionAttachment(file, ready, 12).code, "too_many_images");
  assert.equal(validateVisionAttachment(file, { application_available: false, reason: "not staged" }).code, "vision_unavailable");
  assert.equal(validateVisionAttachment({ ...file, type: "image/gif" }, ready).code, "unsupported_image_type");
  assert.equal(validateVisionAttachment({ ...file, size: 25 * 1024 * 1024 + 1 }, ready).code, "vision_file_too_large");
});

test("attachment prompt framing cannot be broken by a newline in a file name", () => {
  assert.equal(safeAttachmentName("a\nfile.txt"), "a file.txt");
  assert.equal(
    attachmentPromptSuffix([{ inspection: { prompt_text: "inspected hello" } }]),
    "\n\ninspected hello",
  );
});
