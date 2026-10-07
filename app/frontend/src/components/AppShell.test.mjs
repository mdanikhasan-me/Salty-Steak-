import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const componentUrl = new URL("./AppShell.jsx", import.meta.url);
const chatStylesUrl = new URL("../styles/chat.css", import.meta.url);
const designStylesUrl = new URL("../styles/design-r18.css", import.meta.url);

test("the shell keeps workspace navigation without a duplicate product brand", async () => {
  const source = await readFile(componentUrl, "utf8");

  assert.doesNotMatch(source, /className="product-brand"/);
  assert.match(source, /aria-label="Primary workspace"/);
  assert.match(source, /<span>Chat<\/span>/);
  assert.match(source, /<span>Models & training<\/span>/);
});

test("conversation actions use one row surface and a restrained native menu", async () => {
  const [chatStyles, designStyles] = await Promise.all([
    readFile(chatStylesUrl, "utf8"),
    readFile(designStylesUrl, "utf8"),
  ]);

  assert.match(chatStyles, /\.conversation-row--active\s*\{[^}]*background:/s);
  assert.match(chatStyles, /\.conversation-link\s*\{[^}]*background:\s*transparent/s);
  assert.match(designStyles, /\.conversation-row--active \.conversation-link\s*\{[^}]*background:\s*transparent/s);
  assert.match(chatStyles, /\.conversation-actions-menu\s*\{[^}]*box-shadow:\s*none/s);
});

test("compact windows expose the open chat sidebar as an overlay drawer", async () => {
  const styles = await readFile(designStylesUrl, "utf8");
  const compactRules = styles.slice(styles.indexOf("@media (max-width: 960px)"));

  assert.match(compactRules, /\.chat-page--sidebar-open \.chat-sidebar\s*\{[^}]*visibility:\s*visible/s);
  assert.match(compactRules, /\.chat-page--sidebar-open \.chat-sidebar\s*\{[^}]*pointer-events:\s*auto/s);
  assert.match(compactRules, /\.chat-page--sidebar-open \.chat-sidebar-backdrop\s*\{[^}]*display:\s*block/s);
});

test("compact windows expose Models and training navigation as an overlay drawer", async () => {
  const [source, styles] = await Promise.all([
    readFile(componentUrl, "utf8"),
    readFile(designStylesUrl, "utf8"),
  ]);
  const compactRules = styles.slice(styles.indexOf("@media (max-width: 960px)"));

  assert.match(source, /className="workspace-sidebar-backdrop"/);
  assert.match(source, /aria-label="Close Models and training sidebar"/);
  assert.match(compactRules, /\.app-frame--training \.workspace-sidebar\s*\{[^}]*position:\s*absolute/s);
  assert.match(compactRules, /\.app-shell--sidebar-open \.app-frame--training \.workspace-sidebar\s*\{[^}]*visibility:\s*visible/s);
  assert.match(compactRules, /\.app-shell--sidebar-open \.workspace-sidebar-backdrop\s*\{[^}]*display:\s*block/s);
});
