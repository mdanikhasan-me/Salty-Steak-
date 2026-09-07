import assert from "node:assert/strict";
import test from "node:test";
import { activitySummary } from "./responseActivitySummary.mjs";
test("failed or stopped work never becomes a ready response",()=>{
  assert.equal(activitySummary([],{stopped:true}).state,"stopped");
  assert.equal(activitySummary([{id:"x",label:"Native call",state:"failed",detail:"Model unavailable"}]).label,"Could not finish");
});
test("technical events remain in input while useful work is presented concisely",()=>{
  const entries=[{id:"runtime",label:"Preparing local runtime",kind:"runtime",state:"completed"},{id:"search",label:"Read three sources",kind:"research",state:"completed"}];
  const summary=activitySummary(entries);
  assert.deepEqual(summary.steps.map(e=>e.id),["search"]);
  assert.equal(entries.length,2);
});

test("routing diagnostics do not become user-facing progress, but failures survive",()=>{
  const route={id:"route",kind:"decision",label:"Chose the execution route",state:"running"};
  const result=activitySummary([route],{active:true});
  assert.equal(result.label,"Working on your response");
  assert.equal(result.steps.length,0);
  const failure=activitySummary([{...route,state:"failed",detail:"No available tool"}]);
  assert.equal(failure.failure,"No available tool");
  assert.equal(failure.steps.length,1);
});
