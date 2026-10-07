import assert from 'node:assert/strict';
import test from 'node:test';
import {actionEvents,actionPresentation} from './actionPresentation.mjs';
test('a capture uses its own audit record, never a generated image or file URL',()=>{
  const event={action:'screen.capture',status:'succeeded',observation:{audit_record_id:'a/b',screenshot_path:'C:/private.png'}};
  assert.equal(actionPresentation(event).preview,'/api/automation/previews/a%2Fb');
  assert.equal(actionPresentation({...event,status:'failed'}).preview,null);
});
test('file changes require successful readback and recorded counts',()=>{
  const event={action:'files.manage',status:'succeeded',arguments:{operation:'write',path:'D:/app.js'},
    observation:{readback_verified:true,change:{added:2,removed:1,diff:'-old\n+new'}}};
  assert.equal(actionPresentation(event).label,'Edited app.js');
  assert.equal(actionPresentation({...event,status:'failed'}).change,null);
  assert.equal(actionPresentation({...event,observation:{}}).change,null);
});
test('running actions remain running and detail content is bounded',()=>{
  const item=actionPresentation({action:'terminal.execute',status:'running',observation:{stdout:'x'.repeat(50000)}});
  assert.equal(item.state,'running');assert.equal(item.output.length,24000);
});
test('live and saved task records both supply actions',()=>{
  const steps=[{action:'screen.capture'},{action:'respond'}];
  assert.equal(actionEvents({agent_task:{steps}},true).length,1);
  assert.equal(actionEvents({orchestration:{steps}}).length,1);
});
test('uploaded-image analysis retains conversation ownership in its preview URL',()=>{
  const [event]=actionEvents({vision_input_id:'input-1',conversation_id:'chat-1',generation_state:'completed'});
  assert.equal(actionPresentation(event).preview,'/api/chat/conversations/chat-1/vision-previews/input-1');
  const [failed]=actionEvents({vision_input_id:'input-1',conversation_id:'chat-1',generation_state:'failed'});
  assert.equal(actionPresentation(failed).state,'failed');
});
