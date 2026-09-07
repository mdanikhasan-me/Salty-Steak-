import test from "node:test";
import assert from "node:assert/strict";
import { mergePublicPreview as merge } from "./publicResponsePreview.mjs";
const preview=(tail,end,stream='primary')=>({kind:'output',tail_text:tail,stream_character_count:end,stream_id:stream});
test("public response tails merge by verified overlap without duplicates",()=>{
  let s=merge(null,preview('hello',5),'a');s=merge(s,preview('lo world',11),'a');assert.equal(s.text,'hello world');
  assert.equal(merge(s,preview('lo world',11),'a'),s);assert.equal(merge(s,preview('hello',5),'a'),s);
});
test("retries, missed chunks and corrections never join unrelated text",()=>{
  const first=merge(null,preview('hello',5),'a');
  assert.equal(merge(first,preview('bye',3,'retry'),'a').text,'bye');
  assert.equal(merge(first,preview('bye',3),'b').text,'bye');
  const gap=merge(first,preview('later',20),'a');assert.equal(gap.text,'later');assert.equal(gap.partial,true);
  assert.equal(merge(first,preview('wrong!',6),'a').text,'wrong!');
});
test("offsets follow unicode codepoints and reasoning is never exposed",()=>{
  const s=merge(null,preview('hi 😀',4),'a');assert.equal(merge(s,preview('😀 there',10),'a').text,'hi 😀 there');
  assert.equal(merge(s,{kind:'reasoning',tail_text:'private',stream_character_count:7},'a'),null);
  assert.equal(merge(null,preview('long',2),'a'),null);
});
