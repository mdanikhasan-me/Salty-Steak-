import assert from "node:assert/strict";
import test from "node:test";
import {highlightCode} from "./syntaxHighlight.mjs";
test("C++ tokens have distinct styles while markup in strings stays escaped",()=>{
  const html=highlightCode('#include <iostream>\nint main() { std::cout << "<script>alert(1)</script>"; return 0; }','cpp');
  assert.match(html,/hljs-keyword/);assert.match(html,/hljs-string/);assert.match(html,/hljs-number/);assert.doesNotMatch(html,/<script>/);assert.match(html,/&lt;script&gt;/);
});
test("unknown languages and very large blocks stay safely readable",()=>{
  assert.equal(highlightCode('<img src=x onerror=alert(1)>','unknown'),'&lt;img src=x onerror=alert(1)&gt;');
  assert.equal(highlightCode('x'.repeat(80001),'cpp'),'x'.repeat(80001));
});

test("Python declarations, docstrings and operators keep separate safe scopes",()=>{
  const html=highlightCode('import asyncio\n\ndef showcase(x):\n    """<script>Document the function.</script>"""\n    return x + 42','python');
  assert.match(html,/<span class="hljs-type">def<\/span>/);
  assert.match(html,/hljs-doctag/);assert.match(html,/hljs-operator/);
  assert.doesNotMatch(html,/<script>/);assert.match(html,/&lt;script&gt;/);
});
