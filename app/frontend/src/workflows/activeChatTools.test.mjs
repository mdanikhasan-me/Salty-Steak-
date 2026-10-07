import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const chatPageUrl = new URL("../pages/ChatPage.jsx", import.meta.url);
const styleUrls = [
  new URL("../styles/chat.css", import.meta.url),
  new URL("../styles/design-r18.css", import.meta.url),
  new URL("../styles/global.css", import.meta.url),
];

test("active Chat uses Plugins without legacy Calculator or manual URL controls", async () => {
  const source = await readFile(chatPageUrl, "utf8");
  const styles = (await Promise.all(styleUrls.map((url) => readFile(url, "utf8")))).join("\n");

  assert.match(source, /<PluginsPanel/);
  assert.match(source, /openSettings\("plugins"\)/);
  assert.doesNotMatch(source, /runCalculator|calculatorExpression|api\.calculate/);
  assert.doesNotMatch(source, /browserAddress|openBrowser|Search or enter a web address/);
  assert.doesNotMatch(styles, /\.(?:calculator-tool|browser-tool|tool-command|tool-heading|capability-list|capability-row|capability-badge)\b/);
});
