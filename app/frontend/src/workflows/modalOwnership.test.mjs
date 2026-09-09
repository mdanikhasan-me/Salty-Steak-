import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("settings sheets own keyboard focus and restore it when closed", async () => {
  const [hook, dialog, chat, response, connector] = await Promise.all([
    readFile(new URL("../hooks/useModalFocusTrap.js", import.meta.url), "utf8"),
    readFile(new URL("../components/Dialog.jsx", import.meta.url), "utf8"),
    readFile(new URL("../pages/ChatPage.jsx", import.meta.url), "utf8"),
    readFile(new URL("../components/ResponseSettingsSheet.jsx", import.meta.url), "utf8"),
    readFile(new URL("../components/PluginConnectionDialog.jsx", import.meta.url), "utf8"),
  ]);

  assert.match(hook, /event\.key === "Escape"/);
  assert.match(hook, /event\.key !== "Tab"/);
  assert.match(hook, /previous\.focus\(\)/);
  assert.match(dialog, /className="dialog-layer"/);
  assert.match(dialog, /useModalFocusTrap\(\{ active: open, onClose, initialFocusSelector:/);
  assert.match(dialog, /compact \? "\[data-dialog-cancel\]"/);
  assert.match(dialog, /role="dialog"/);
  assert.match(dialog, /aria-modal="true"/);
  assert.match(dialog, /createPortal/);
  assert.match(dialog, /document\.body/);
  assert.doesNotMatch(dialog, /showModal\(/);
  assert.match(chat, /useModalFocusTrap\(\{ active, onClose \}\)/);
  assert.match(response, /useModalFocusTrap\(\{ active: !embedded, onClose \}\)/);
  const settings = await readFile(new URL("../components/WorkspaceSettings.jsx", import.meta.url), "utf8");
  assert.match(settings, /useModalFocusTrap\(\{ active, onClose \}\)/);
  assert.match(connector, /useModalFocusTrap\(\{ onClose, canClose: !busy \}\)/);
});
