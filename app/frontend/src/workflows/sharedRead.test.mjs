import test from "node:test";
import assert from "node:assert/strict";
import { createSharedRead } from "./sharedRead.mjs";
test("overlapping readers share a request and mutations invalidate the snapshot",async()=>{
  let count=0,time=0;
  const read=createSharedRead(async()=>++count,{freshFor:300,now:()=>time});
  assert.deepEqual(await Promise.all([read("a"),read("a")]),[1,1]);
  time=200;assert.equal(await read("a"),1);
  read.invalidate("a");assert.equal(await read("a"),2);
  time=600;assert.equal(await read("a"),3);
});
test("a transient failure never poisons later reads",async()=>{
  let calls=0;const read=createSharedRead(async()=>{if(!calls++)throw Error("offline");return "reconnected";});
  await assert.rejects(read("a"),/offline/);
  assert.equal(await read("a"),"reconnected");
});
