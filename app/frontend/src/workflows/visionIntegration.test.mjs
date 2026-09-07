import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../../../../", import.meta.url);

test("Chat composes several images into one permission-bound vision input", async () => {
  const [page, client] = await Promise.all([
    readFile(new URL("app/frontend/src/pages/ChatPage.jsx", root), "utf8"),
    readFile(new URL("app/frontend/src/api/client.js", root), "utf8"),
  ]);

  assert.match(page, /chatStatus\?\.vision\?\.application_available/);
  assert.match(page, /validateVisionAttachment/);
  assert.match(page, /buildVisionContactSheet\(imageAttachments\)/);
  assert.match(page, /api\.stageVisionInput\(contactSheet\)/);
  assert.match(page, /api\.analyzeVisionInput/);
  assert.match(page, /restoreFailedDraft\(conversationId, content, originalAttachments\)/);
  assert.match(page, /type="file"[\s\S]{0,80}\bmultiple\b/);
  assert.doesNotMatch(page, /imageAttachmentInputRef/);
  assert.match(client, /form\.append\("user_confirmed", "true"\)/);
  assert.match(client, /\/chat\/vision-input/);
  assert.match(client, /\/vision-analyses/);
  assert.match(page, /onAnalyzeCapture/);
  assert.match(page, /stageScreenCaptureForVision\(captureResult\.audit_record_id\)/);
  assert.match(page, /Write what you want Base Steak 2\.0 to analyze/);
  assert.doesNotMatch(page, /stageScreenCaptureForVision[\s\S]{0,80}invokeAutomation/);
});
