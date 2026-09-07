import test from "node:test";
import assert from "node:assert/strict";
import { createConversationCache } from "./conversationCache.mjs";
test("cached navigation has strict identity and a bounded least-recently-used working set",()=>{
  const c=createConversationCache(2);c.set({id:'a'});c.set({id:'b'});assert.equal(c.get('other'),null);
  assert.equal(c.get('a').id,'a');c.set({id:'c'});assert.equal(c.get('b'),null);assert.equal(c.get('a').id,'a');c.delete('a');assert.equal(c.get('a'),null);
});
