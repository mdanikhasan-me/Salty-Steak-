import assert from "node:assert/strict";
import test from "node:test";

import { parseRichTextBlocks, tokenizeInline } from "./richText.mjs";

test("named file fences remain a single code block in the transcript", () => {
  assert.deepEqual(parseRichTextBlocks("```python file=add.py\ndef add(a, b):\n    return a + b\n```"), [
    { type: "code", language: "python", content: "def add(a, b):\n    return a + b" },
  ]);
});

test("a bare address is a link", () => {



  assert.deepEqual(tokenizeInline("See https://shop.example.com/item now"), [
    { type: "text", content: "See " },
    {
      type: "link",
      content: "https://shop.example.com/item",
      href: "https://shop.example.com/item",
    },
    { type: "text", content: " now" },
  ]);
});

test("a sentence ending in an address keeps its full stop", () => {
  const tokens = tokenizeInline("Buy it at https://shop.example.com/item.");
  assert.equal(tokens[1].href, "https://shop.example.com/item");
  assert.equal(tokens[2].content, ".");
});

test("a markdown link still wins over the bare form", () => {
  assert.deepEqual(tokenizeInline("[Ryans](https://www.ryans.com/a)"), [
    { type: "link", content: "Ryans", href: "https://www.ryans.com/a" },
  ]);
});

test("rich text groups common assistant markdown into semantic blocks", () => {
  assert.deepEqual(
    parseRichTextBlocks("## Result\n\n- One\n- Two\n\n```js\nconst ready = true;\n```"),
    [
      { type: "heading", level: 2, content: "Result" },
      { type: "unordered-list", items: ["One", "Two"] },
      { type: "code", language: "js", content: "const ready = true;" },
    ],
  );
});

test("rich text supports ordered lists, quotes, and multiline paragraphs", () => {
  assert.deepEqual(
    parseRichTextBlocks("First line\ncontinues here\n\n1. Alpha\n2. Beta\n\n> Local\n> only"),
    [
      { type: "paragraph", content: "First line\ncontinues here" },
      { type: "ordered-list", items: ["Alpha", "Beta"] },
      { type: "quote", content: "Local\nonly" },
    ],
  );
});

test("inline formatting keeps unsupported and unsafe link syntax as text", () => {
  assert.deepEqual(tokenizeInline("Use **bold**, `code`, and [safe](https://example.com)."), [
    { type: "text", content: "Use " },
    { type: "strong", content: "bold" },
    { type: "text", content: ", " },
    { type: "code", content: "code" },
    { type: "text", content: ", and " },
    { type: "link", content: "safe", href: "https://example.com" },
    { type: "text", content: "." },
  ]);
  assert.deepEqual(tokenizeInline("[local](file:///secret)"), [
    { type: "text", content: "[local](file:///secret)" },
  ]);
});
