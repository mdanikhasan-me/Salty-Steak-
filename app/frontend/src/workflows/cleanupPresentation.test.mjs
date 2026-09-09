import test from "node:test";
import assert from "node:assert/strict";
import { cleanupPresentation, hostSafeAssistantContent, hostActionProposalForMessage, HOST_ACTION_SCHEMA } from "./hostActions.mjs";

const proposal={kind:"system.clean_temp",state:"pending_review"};
test("live cleanup overrides pending proposal text without claiming completion",()=>{
  assert.equal(hostSafeAssistantContent({content:"The Windows temporary-folder cleanup is ready for your review."},proposal,{state:"running"}),"");
  assert.equal(cleanupPresentation(proposal,{state:"running"}).label,"Cleaning temporary files");
  assert.equal(cleanupPresentation(proposal,{state:"stop_requested"}).label,"Stopping cleanup");
});
test("completed cleanup reports partial results and retains inaccessible roots",()=>{
  const report=cleanupPresentation(proposal,{state:"completed",result:{result:{deleted_files:21302,reclaimed_bytes:4080074392,skipped_entries:1028,roots:[{name:"Prefetch",status:"inaccessible"}]}}});
  assert.match(report.label,/skipped/);assert.match(report.summary,/21,302 files/);assert.match(report.summary,/3.8 GiB/);assert.equal(report.roots[0].status,"inaccessible");
});
test("completed without results is unknown, never a successful cleanup claim",()=>{
  assert.equal(cleanupPresentation(proposal,{state:"completed"}).label,"Cleanup result unavailable");
});
test("persisted result survives proposal normalization after restart",()=>{
  const normalized=hostActionProposalForMessage({role:"assistant",technical_details:{host_action_proposal:{schema:HOST_ACTION_SCHEMA,...proposal,state:"completed",summary:"Clean temp",arguments:{},result:{deleted_files:0,reclaimed_bytes:0,skipped_entries:3}}}});
  assert.match(cleanupPresentation(normalized).summary,/0 files removed/);
  assert.match(cleanupPresentation(normalized).label,/skipped/);
});
