import test from "node:test";
import assert from "node:assert/strict";
import {companionIsWorking} from "./companionActivity.mjs";
test("companion follows actual running work and stops on completion, failure, or disconnected service",()=>{
  const task={id:"a",type:"chat_generation",state:"running"};
  assert.equal(companionIsWorking({a:task}),true);
  for(const state of ["queued","completed","failed","interrupted"])assert.equal(companionIsWorking({a:{...task,state}}),false);
  assert.equal(companionIsWorking({a:task},false),false);
  assert.equal(companionIsWorking({a:{...task,type:"training"}}),false);
});
test("another active conversation keeps the companion working; user approval pauses it",()=>{
  const task={type:"chat_generation",state:"running"};
  assert.equal(companionIsWorking({a:{...task,state:"completed"},b:task}),true);
  assert.equal(companionIsWorking({a:{...task,result:{agent_task:{state:"waiting"}}}}),false);
});
