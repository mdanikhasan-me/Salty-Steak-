import test from "node:test";
import assert from "node:assert/strict";
import { createLatestWriter } from "./latestWriter.mjs";
test("rapid preference edits cannot arrive out of order or lose other fields",async()=>{
  let unblock;const calls=[];
  const write=createLatestWriter(async value=>{calls.push(value);if(calls.length===1)await new Promise(r=>unblock=r);return value;});
  const a=write({workspaceMode:'chat'}),b=write({workspaceMode:'code',theme:'warm'}),c=write({workspaceMode:'agent'});
  assert.equal(calls.length,1);unblock();await Promise.all([a,b,c]);
  assert.deepEqual(calls,[{workspaceMode:'chat'},{workspaceMode:'agent',theme:'warm'}]);
});
test("later saves remain available after a failed write",async()=>{
  let calls=0;const write=createLatestWriter(async value=>{if(!calls++)throw Error('offline');return value;});
  await assert.rejects(write({theme:'dark'}),/offline/);
  assert.deepEqual(await write({theme:'warm'}),{theme:'warm'});
});
