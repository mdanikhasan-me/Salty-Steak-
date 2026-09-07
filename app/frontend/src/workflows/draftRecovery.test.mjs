import test from "node:test";
import assert from "node:assert/strict";
import { recoverConversationDraft } from "./draftRecovery.mjs";
test("late failure restores only its original conversation",()=>{
  const drafts=new Map([['a',{draft:''}],['b',{draft:'new thought'}]]);
  const result=recoverConversationDraft({drafts,ownerId:'a',selectedId:'b',current:{draft:'new thought'},content:'failed message'});
  assert.equal(result,null);assert.equal(drafts.get('a').draft,'failed message');assert.equal(drafts.get('b').draft,'new thought');
});
test("late failure cannot overwrite newer typing even in the same conversation",()=>{
  const drafts=new Map();
  assert.equal(recoverConversationDraft({drafts,ownerId:'a',selectedId:'a',current:{draft:'next question'},content:'failed'}),null);
  assert.equal(drafts.size,0);
});
test("an empty original composer recovers its prompt and attachments",()=>{
  const drafts=new Map(),attachments=[{name:'example.py'}];
  const restored=recoverConversationDraft({drafts,ownerId:'a',selectedId:'a',current:{draft:''},content:'failed',attachments});
  assert.equal(restored.draft,'failed');assert.deepEqual(restored.attachments,attachments);
});
