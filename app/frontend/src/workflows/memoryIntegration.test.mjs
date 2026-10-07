import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../../../../", import.meta.url);

test("Chat exposes opt-in memory without globally persisting conversation instructions", async () => {
  const [page, client, memoryPanel] = await Promise.all([
    readFile(new URL("app/frontend/src/pages/ChatPage.jsx", root), "utf8"),
    readFile(new URL("app/frontend/src/api/client.js", root), "utf8"),
    readFile(new URL("app/frontend/src/components/MemoryPanel.jsx", root), "utf8"),
  ]);

  assert.match(page, /composerCommandSuggestions\(draft\)/);
  assert.match(page, /action\?\.type === "save_memory"/);
  assert.match(page, /api\.saveConversationMemory\(conversationId\)/);
  assert.match(page, /delete persistentSettings\.system_prompt/);
  assert.match(page, /conversationInstructionDraftsRef = useRef\(session.instructions\)/);
  assert.match(page, /conversationInstructionDraftsRef\.current\.set/);
  assert.match(page, /localDraft === undefined[\s\S]*fetched\?\.system_prompt/);
  assert.match(page, /locallyCreatedConversationIdsRef = useRef\(new Set\(\)\)/);
  assert.doesNotMatch(page, /conversationSelectionExists\(conversations, selectedId\)/);
  assert.match(page, /Number\(error\?\.status \|\| 0\) === 404/);
  assert.match(page, /const fallback = conversations\.find/);
  assert.match(page, /const openConversation = useCallback\(async \(conversationId\)/);
  assert.match(page, /const payload = await api\.getConversation\(wanted\)/);
  assert.match(page, /if \(String\(selectedIdRef\.current\) !== wanted\) return/);
  assert.match(page, /onSelect=\{\(conversationId\) => void openConversation\(conversationId\)\}/);
  assert.ok(
    (page.match(/locallyCreatedConversationIdsRef\.current\.add\(String\(created\.id\)\)/g) || []).length >= 3,
  );
  assert.match(page, /settingsView === "memory"/);
  const inspector = page.slice(page.indexOf("{inspectorOpen && !showAbout"));
  assert.match(
    inspector,
    /settingsView === "plugins"[\s\S]*settingsView === "memory"[\s\S]*<MemoryPanel \/>[\s\S]*<ResponseSettingsSheet/,
  );
  const activity = page.slice(
    page.indexOf("{activityWorkspaceOpen && !showAbout"),
    page.indexOf("{responseDetailsFor && !showAbout"),
  );
  assert.doesNotMatch(activity, /MemoryPanel|settingsView === "memory"/);
  const normalSend = page.slice(
    page.indexOf("const sendExactMessage = useCallback"),
    page.indexOf("const analyzeSelectedImages = useCallback"),
  );
  const imageSend = page.slice(
    page.indexOf("const analyzeSelectedImages = useCallback"),
    page.indexOf("function newChat"),
  );
  assert.match(normalSend, /action\?\.type === "save_memory"[\s\S]*api\.saveChatMemory/);
  assert.doesNotMatch(imageSend, /action\?\.type === "save_memory"|api\.saveChatMemory/);

  assert.match(client, /listChatMemories/);
  assert.match(client, /saveChatMemory/);
  assert.match(client, /saveConversationMemory/);
  assert.match(client, /forgetChatMemory/);
  assert.match(client, /clearChatMemories/);

  assert.match(memoryPanel, /Automatic saving is off/);
  assert.match(memoryPanel, /\/save mem/);
  assert.match(memoryPanel, /api\.forgetChatMemory/);
  assert.match(memoryPanel, /api\.clearChatMemories/);
});
