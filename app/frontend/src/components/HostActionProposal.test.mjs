import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("image proposals confirm, report progress, stop, retry, and never borrow Cooking UI", async () => {
  const [proposal, message, page] = await Promise.all([
    readFile(new URL("./HostActionProposal.jsx", import.meta.url), "utf8"),
    readFile(new URL("./ChatMessage.jsx", import.meta.url), "utf8"),
    readFile(new URL("../pages/ChatPage.jsx", import.meta.url), "utf8"),
  ]);

  for (const label of [
    "Create this image?",
    "Create image",
    "Image ready",
    "Retry",
    "Stop",
    "New seed",
    "Image model",
    "Aspect ratio",
    "Resolution",
    "Quality",
  ]) {
    assert.match(proposal, new RegExp(label.replace("?", "\\?")));
  }
  assert.match(proposal, /measurableProgress/);
  assert.match(proposal, /activity-phrase activity-phrase--compact/);
  assert.match(proposal, /image-generation-stage/);
  assert.match(proposal, /const running = isActive\(operation\)/);
  assert.match(proposal, /\{image && running \? \(/);
  assert.match(proposal, /return "Completed"/);
  assert.match(proposal, /return `Image generation \$\{state\}`/);
  assert.match(proposal, /Action failed/);
  assert.match(proposal, /onConfirm\?\.\(proposal/);
  assert.match(proposal, /onStop\(operation\)/);
  assert.match(proposal, /createFreshImageSeed/);
  assert.match(proposal, /model_id: imageModelId/);
  assert.match(proposal, /width: imageCanvasSize\.width/);
  assert.match(proposal, /height: imageCanvasSize\.height/);
  assert.doesNotMatch(proposal, /CookingStatus|CookingActivity|Cooking/);

  assert.match(message, /visibleContent\.answer && !generatedImage/);
  assert.match(message, /actionProposal && !actionDismissed && !generatedImage/);
  assert.match(page, /isImageGenerationOperation\(activeGeneration\)/);
  assert.match(page, /selectedConversationBusy/);
  assert.match(page, /confirmHostActionProposal\(message, proposal, settings\)/);
});

test("exact file proposals use a dedicated recoverable confirmation instead of a command", async () => {
  const [proposal, page, client] = await Promise.all([
    readFile(new URL("./HostActionProposal.jsx", import.meta.url), "utf8"),
    readFile(new URL("../pages/ChatPage.jsx", import.meta.url), "utf8"),
    readFile(new URL("../api/client.js", import.meta.url), "utf8"),
  ]);
  assert.match(proposal, /filesystem\.trash_file/);
  assert.match(proposal, /Move this exact file to Recycle Bin\?/);
  assert.match(proposal, /Windows Recycle Bin/);
  assert.match(proposal, /Shell command/);
  assert.match(proposal, /Not used/);
  assert.match(page, /chat_host_action_execution/);
  assert.match(page, /confirmHostActionProposal/);
  assert.match(client, /confirmHostActionProposal/);
});

test("Windows temporary cleanup has an exact scope and locked-file behavior", async () => {
  const proposal = await readFile(new URL("./HostActionProposal.jsx", import.meta.url), "utf8");
  assert.match(proposal, /system\.clean_temp/);
  assert.match(proposal, /Clean temporary files\?/);
  assert.match(proposal, /!directAction && reviewable/);
  assert.match(proposal, /folders themselves stay in place/);
  assert.match(proposal, /Files in use/);
  assert.match(proposal, /entries are skipped/);
  assert.match(proposal, /Clean temporary files/);
  assert.match(proposal, /cleanupAcknowledged/);
  assert.match(proposal, /deleted permanently/);
  assert.match(proposal, /TEMP_CLEANUP_CONFIRMATION/);
  assert.match(proposal, /type=\{tempCleanup \? "button" : "submit"\}/);
});
