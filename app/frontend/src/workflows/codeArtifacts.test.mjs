import assert from "node:assert/strict";
import test from "node:test";

import { codeArtifactsFromText, normaliseNamedCodeFences } from "./codeArtifacts.mjs";

test("a model-authored script becomes a real named file artifact", () => {
  const files = codeArtifactsFromText(
    "Here it is.\n\n```python file=cleanup.py\nprint('ready')\n```",
  );

  assert.deepEqual(files, [
    {
      id: "1-cleanup.py",
      filename: "cleanup.py",
      language: "python",
      content: "print('ready')\n",
      bytes: 15,
    },
  ]);
});

test("only explicitly named code blocks become files", () => {
  const files = codeArtifactsFromText(
    "```powershell\nWrite-Output ready\n```\n```json filename=../config.json\n{}\n```",
  );

  assert.deepEqual(files.map((file) => file.filename), ["config.json"]);
  assert.equal(files[0].content, "{}\n");
});

test("ordinary prose and empty fences create no fake files", () => {
  assert.deepEqual(codeArtifactsFromText("Use Python for this."), []);
  assert.deepEqual(codeArtifactsFromText("```python\n\n```"), []);
  assert.deepEqual(codeArtifactsFromText("```python\nprint('example')\n```"), []);
});

test("a local model's separate file marker becomes fence metadata, not executable file content", () => {
  const response = "```python\n\nfile=add.py\ndef add(a, b):\n    return a + b\n```";
  const [file] = codeArtifactsFromText(response);
  assert.equal(file.filename, "add.py");
  assert.equal(file.content, "def add(a, b):\n    return a + b\n");
  assert.match(normaliseNamedCodeFences(response), /^```python file=add.py\ndef add/);
  assert.deepEqual(codeArtifactsFromText("```python\nfile = 'notes.txt'\nprint(file)\n```"), []);
});
