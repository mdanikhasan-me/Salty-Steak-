import test from "node:test";
import assert from "node:assert/strict";
import { pollOperations } from "./operationPolling.mjs";
const running={id:"work",type:"chat_generation",state:"running"};

test("an active operation reports loss of service instead of staying online",async()=>{
  const error=new Error("Service stopped");
  await assert.rejects(pollOperations({work:running},{getOperation:async()=>{throw error;}}),error);
});
test("missing persisted operations stop blocking the composer after restart",async()=>{
  const result=await pollOperations({work:running},{getOperation:async()=>{throw {status:404};}});
  assert.equal(result[0].id,"work");assert.equal(result[0].state,"interrupted");
});
test("partial failures preserve successful updates, recovery accepts the saved terminal state",async()=>{
  const client={getOperation:async id=>{if(id==="other")throw Error("temporary");return {...running,state:"completed"};}};
  assert.equal((await pollOperations({work:running,other:{...running,id:"other"}},client))[0].state,"completed");
});
