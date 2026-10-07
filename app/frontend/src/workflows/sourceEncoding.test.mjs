import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import test from "node:test";

const SOURCE_ROOT = resolve("app/frontend/src");
const TEXT_EXTENSIONS = new Set([".css", ".js", ".jsx", ".mjs"]);
const forbidden = [
  String.fromCodePoint(0xfffd),
  String.fromCodePoint(0x00c2),
  String.fromCodePoint(0x00c3),
  `${String.fromCodePoint(0x00e2)}${String.fromCodePoint(0x20ac)}`,
  `${String.fromCodePoint(0x00f0)}${String.fromCodePoint(0x0178)}`,
];

async function sourceFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map(async (entry) => {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return TEXT_EXTENSIONS.has(extname(entry.name)) ? [path] : [];
  }));
  return nested.flat();
}

test("frontend source contains no replacement glyphs or common UTF-8 mojibake", async () => {
  const failures = [];
  for (const path of await sourceFiles(SOURCE_ROOT)) {
    const source = await readFile(path, "utf8");
    for (const token of forbidden) {
      if (source.includes(token)) failures.push(`${path}: U+${token.codePointAt(0).toString(16).toUpperCase()}`);
    }
  }

  assert.deepEqual(failures, []);
});
